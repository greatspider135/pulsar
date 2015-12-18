from functools import partial
from collections import namedtuple
from copy import copy
from urllib.parse import urlparse, urljoin

from pulsar import OneTime, task, ProtocolConsumer
from pulsar.apps.ws import WebSocketProtocol, WS
from pulsar.utils.internet import is_tls
from pulsar.utils.httpurl import REDIRECT_CODES, requote_uri, SimpleCookie

from pulsar import PulsarException


def noerror(callback):
    '''Decorator to run a callback of a :class:`.EventHandler`
    only when no errors occur
    '''
    def _(*response, **kw):
        if response[-1] and not kw.get('exc'):
            return callback(*response)

    return _


def response_content(resp, exc=None, **kw):
    b = resp.parser.recv_body()
    if b or resp._content is None:
        resp._content = resp._content + b if resp._content else b
    return resp._content


def _consumer(response, consumer):
    if response is not None:
        consumer = response
    return consumer


def start_request(request, conn):
    consumer = conn.current_consumer()
    # bind request-specific events
    consumer.bind_events(**request.inp_params)
    if request.stream:
        consumer.bind_event('data_processed', consumer.raw)
        consumer.start(request)
        yield from consumer.events['on_headers']
    else:
        consumer.bind_event('data_processed', response_content)
        consumer.start(request)
        yield from consumer.on_finished

    if consumer.request_again:
        if isinstance(consumer.request_again, Exception):
            raise consumer.request_again
        elif isinstance(consumer.request_again, ProtocolConsumer):
            consumer = consumer.request_again
    return consumer


class request_again(namedtuple('request_again', 'method url params')):

    @property
    def status_code(self):
        return -1

    @property
    def headers(self):
        return ()


class TooManyRedirects(PulsarException):

    def __init__(self, response):
        self.response = response


class WebSocketClient(WebSocketProtocol):
    status_code = 101

    @property
    def _request(self):
        return self.handshake._request

    @property
    def headers(self):
        return self.handshake.headers

    def __getattr__(self, name):
        if not name.startswith('__'):
            return getattr(self.handshake, name)
        else:
            raise AttributeError("'%s' object has no attribute '%s'" %
                                 (self.__class__.__name__, name))


@noerror
def handle_redirect(response, exc=None):
    if (response.status_code in REDIRECT_CODES and
            'location' in response.headers and
            response._request.allow_redirects):
        # put at the end of the pile
        response.bind_event('post_request', _do_redirect)


@noerror
def _do_redirect(response, exc=None):
    request = response.request
    # done with current response
    url = response.headers.get('location')
    # Handle redirection without scheme (see: RFC 1808 Section 4)
    if url.startswith('//'):
        parsed_rurl = urlparse(request.full_url)
        url = '%s:%s' % (parsed_rurl.scheme, url)
    # Facilitate non-RFC2616-compliant 'location' headers
    # (e.g. '/path/to/resource' instead of
    # 'http://domain.tld/path/to/resource')
    if not urlparse(url).netloc:
        url = urljoin(request.full_url,
                      # Compliant with RFC3986, we percent
                      # encode the url.
                      requote_uri(url))
    history = request.history
    if history and len(history) >= request.max_redirects:
        response.request_again = TooManyRedirects(response)
    else:
        params = request.inp_params.copy()
        params['history'] = copy(history) if history else []
        params['history'].append(response)
        if response.status_code == 303:
            method = 'GET'
            params.pop('data', None)
            params.pop('files', None)
        else:
            method = request.method
        response.request_again = request_again(method, url, params)


@noerror
def handle_cookies(response, exc=None):
    '''Handle response cookies.
    '''
    headers = response.headers
    request = response.request
    client = request.client
    response._cookies = c = SimpleCookie()
    if 'set-cookie' in headers or 'set-cookie2' in headers:
        for cookie in (headers.get('set-cookie2'),
                       headers.get('set-cookie')):
            if cookie:
                c.load(cookie)
        if client.store_cookies:
            client.cookies.extract_cookies(response, request)


@noerror
def handle_100(response, exc=None):
    '''Handle Except: 100-continue.

    This is a ``on_header`` hook which checks if the request headers
    have the ``Expect: 100-continue`` value. If so add a ``on_headers``
    callback to handle the response from the server.
    '''
    request = response.request
    if (request.headers.has('expect', '100-continue') and
            response.status_code == 100):
        response.bind_event('on_headers', _write_body)


@noerror
def _write_body(response, exc=None):
    if response.status_code == 100:
        response.request.new_parser()
        if response.request.data:
            response.write(response.request.data)


@noerror
def handle_101(response, exc=None):
    '''Websocket upgrade as ``on_headers`` event.'''

    if response.status_code == 101:
        connection = response.connection
        request = response._request
        handler = request.websocket_handler
        if not handler:
            handler = WS()
        parser = request.client.frame_parser(kind=1)
        body = response.recv_body()
        connection.upgrade(partial(WebSocketClient, response, handler, parser))
        response.finished()
        consumer = connection.current_consumer()
        consumer.data_received(body)
        response.request_again = consumer


class Tunneling:
    '''A pre request callback for handling proxy tunneling.

    If Tunnelling is required, it writes the CONNECT headers and abort
    the writing of the actual request until headers from the proxy server
    are received.
    '''
    def __init__(self, loop):
        assert loop
        self._loop = loop

    @noerror
    def __call__(self, response, exc=None):
        # the pre_request handler
        request = response._request
        if request:
            tunnel = request._tunnel
            if tunnel:
                if getattr(request, '_apply_tunnel', False):
                    # if transport is not SSL already
                    transport = response.transport
                    if not transport.get_extra_info('sslcontext'):
                        if not is_tls(transport.get_extra_info('socket')):
                            response._request = tunnel
                            response.bind_event('on_headers', self.on_headers)
                else:
                    # Append self again as pre_request
                    request._apply_tunnel = True
                    response.bind_event('pre_request', self)

    @noerror
    def on_headers(self, response, exc=None):
        '''Called back once the headers have arrived.'''
        if response.status_code == 200:
            response.bind_event('post_request', self._switch_to_ssl)
            response.finished()

    @task
    def _switch_to_ssl(self, response, exc=None, **kw):
        '''Wrap the transport for SSL communication.
        '''
        if exc:
            return
        response.transport.pause_reading()
        yield None
        request = response._request.request
        connection = response._connection
        loop = connection._loop
        sock = connection.sock
        # set a new connection_made event
        connection.events['connection_made'] = OneTime(loop=loop)
        connection._processed -= 1
        connection.producer._requests_processed -= 1

        loop._make_ssl_transport(sock, connection, request._ssl,
                                 server_hostname=request._netloc)
        yield from connection.event('connection_made')
        consumer = yield from start_request(request, connection)
        response.request_again = consumer

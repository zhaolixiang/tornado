#!/usr/bin/env python
from __future__ import absolute_import, division, print_function, with_statement

from tornado.concurrent import is_future
from tornado.escape import utf8, _unicode
from tornado.httpclient import HTTPResponse, HTTPError, AsyncHTTPClient, main, _RequestProxy
from tornado import httputil
from tornado.http1connection import HTTP1Connection
from tornado.iostream import IOStream, SSLIOStream, StreamClosedError
from tornado.netutil import Resolver, OverrideResolver
from tornado.log import gen_log
from tornado import stack_context

import base64
import collections
import copy
import functools
import os.path
import re
import socket
import ssl
import sys

try:
    from io import BytesIO  # python 3
except ImportError:
    from cStringIO import StringIO as BytesIO  # python 2

try:
    import urlparse  # py2
except ImportError:
    import urllib.parse as urlparse  # py3

_DEFAULT_CA_CERTS = os.path.dirname(__file__) + '/ca-certificates.crt'


class SimpleAsyncHTTPClient(AsyncHTTPClient):
    """Non-blocking HTTP client with no external dependencies.

    This class implements an HTTP 1.1 client on top of Tornado's IOStreams.
    It does not currently implement all applicable parts of the HTTP
    specification, but it does enough to work with major web service APIs.

    Some features found in the curl-based AsyncHTTPClient are not yet
    supported.  In particular, proxies are not supported, connections
    are not reused, and callers cannot select the network interface to be
    used.
    """
    def initialize(self, io_loop, max_clients=10,
                   hostname_mapping=None, max_buffer_size=104857600,
                   resolver=None, defaults=None, max_header_size=None):
        """Creates a AsyncHTTPClient.

        Only a single AsyncHTTPClient instance exists per IOLoop
        in order to provide limitations on the number of pending connections.
        force_instance=True may be used to suppress this behavior.

        max_clients is the number of concurrent requests that can be
        in progress.  Note that this arguments are only used when the
        client is first created, and will be ignored when an existing
        client is reused.

        hostname_mapping is a dictionary mapping hostnames to IP addresses.
        It can be used to make local DNS changes when modifying system-wide
        settings like /etc/hosts is not possible or desirable (e.g. in
        unittests).

        max_buffer_size is the number of bytes that can be read by IOStream. It
        defaults to 100mb.
        """
        super(SimpleAsyncHTTPClient, self).initialize(io_loop,
                                                      defaults=defaults)
        self.max_clients = max_clients
        self.queue = collections.deque()
        self.active = {}
        self.waiting = {}
        self.max_buffer_size = max_buffer_size
        self.max_header_size = max_header_size
        if resolver:
            self.resolver = resolver
            self.own_resolver = False
        else:
            self.resolver = Resolver(io_loop=io_loop)
            self.own_resolver = True
        if hostname_mapping is not None:
            self.resolver = OverrideResolver(resolver=self.resolver,
                                             mapping=hostname_mapping)

    def close(self):
        super(SimpleAsyncHTTPClient, self).close()
        if self.own_resolver:
            self.resolver.close()

    def fetch_impl(self, request, callback):
        key = object()
        self.queue.append((key, request, callback))
        if not len(self.active) < self.max_clients:
            timeout_handle = self.io_loop.add_timeout(
                self.io_loop.time() + min(request.connect_timeout,
                                          request.request_timeout),
                functools.partial(self._on_timeout, key))
        else:
            timeout_handle = None
        self.waiting[key] = (request, callback, timeout_handle)
        self._process_queue()
        if self.queue:
            gen_log.debug("max_clients limit reached, request queued. "
                          "%d active, %d queued requests." % (
                              len(self.active), len(self.queue)))

    def _process_queue(self):
        with stack_context.NullContext():
            while self.queue and len(self.active) < self.max_clients:
                key, request, callback = self.queue.popleft()
                if key not in self.waiting:
                    continue
                self._remove_timeout(key)
                self.active[key] = (request, callback)
                release_callback = functools.partial(self._release_fetch, key)
                self._handle_request(request, release_callback, callback)

    def _handle_request(self, request, release_callback, final_callback):
        _HTTPConnection(self.io_loop, self, request, release_callback,
                        final_callback, self.max_buffer_size, self.resolver,
                        self.max_header_size)

    def _release_fetch(self, key):
        del self.active[key]
        self._process_queue()

    def _remove_timeout(self, key):
        if key in self.waiting:
            request, callback, timeout_handle = self.waiting[key]
            if timeout_handle is not None:
                self.io_loop.remove_timeout(timeout_handle)
            del self.waiting[key]

    def _on_timeout(self, key):
        request, callback, timeout_handle = self.waiting[key]
        self.queue.remove((key, request, callback))
        timeout_response = HTTPResponse(
            request, 599, error=HTTPError(599, "Timeout"),
            request_time=self.io_loop.time() - request.start_time)
        self.io_loop.add_callback(callback, timeout_response)
        del self.waiting[key]


class _HTTPConnection(httputil.HTTPMessageDelegate):
    _SUPPORTED_METHODS = set(["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

    def __init__(self, io_loop, client, request, release_callback,
                 final_callback, max_buffer_size, resolver,
                 max_header_size):
        self.start_time = io_loop.time()
        self.io_loop = io_loop
        self.client = client
        self.request = request
        self.release_callback = release_callback
        self.final_callback = final_callback
        self.max_buffer_size = max_buffer_size
        self.resolver = resolver
        self.max_header_size = max_header_size
        self.code = None
        self.headers = None
        self.chunks = []
        self._decompressor = None
        # Timeout handle returned by IOLoop.add_timeout
        self._timeout = None
        self._sockaddr = None
        with stack_context.ExceptionStackContext(self._handle_exception):
            self.parsed = urlparse.urlsplit(_unicode(self.request.url))
            if self.parsed.scheme not in ("http", "https"):
                raise ValueError("Unsupported url scheme: %s" %
                                 self.request.url)
            # urlsplit results have hostname and port results, but they
            # didn't support ipv6 literals until python 2.7.
            netloc = self.parsed.netloc
            if "@" in netloc:
                userpass, _, netloc = netloc.rpartition("@")
            match = re.match(r'^(.+):(\d+)$', netloc)
            if match:
                host = match.group(1)
                port = int(match.group(2))
            else:
                host = netloc
                port = 443 if self.parsed.scheme == "https" else 80
            if re.match(r'^\[.*\]$', host):
                # raw ipv6 addresses in urls are enclosed in brackets
                host = host[1:-1]
            self.parsed_hostname = host  # save final host for _on_connect

            if request.allow_ipv6:
                af = socket.AF_UNSPEC
            else:
                # We only try the first IP we get from getaddrinfo,
                # so restrict to ipv4 by default.
                af = socket.AF_INET

            timeout = min(self.request.connect_timeout, self.request.request_timeout)
            if timeout:
                self._timeout = self.io_loop.add_timeout(
                    self.start_time + timeout,
                    stack_context.wrap(self._on_timeout))
            self.resolver.resolve(host, port, af, callback=self._on_resolve)

    def _on_resolve(self, addrinfo):
        if self.final_callback is None:
            # final_callback is cleared if we've hit our timeout
            return
        self.stream = self._create_stream(addrinfo)
        self.stream.set_close_callback(self._on_close)
        # ipv6 addresses are broken (in self.parsed.hostname) until
        # 2.7, here is correctly parsed value calculated in __init__
        self._sockaddr = addrinfo[0][1]
        self.stream.connect(self._sockaddr, self._on_connect,
                            server_hostname=self.parsed_hostname)

    def _create_stream(self, addrinfo):
        af = addrinfo[0][0]
        if self.parsed.scheme == "https":
            ssl_options = {}
            if self.request.validate_cert:
                ssl_options["cert_reqs"] = ssl.CERT_REQUIRED
            if self.request.ca_certs is not None:
                ssl_options["ca_certs"] = self.request.ca_certs
            else:
                ssl_options["ca_certs"] = _DEFAULT_CA_CERTS
            if self.request.client_key is not None:
                ssl_options["keyfile"] = self.request.client_key
            if self.request.client_cert is not None:
                ssl_options["certfile"] = self.request.client_cert

            # SSL interoperability is tricky.  We want to disable
            # SSLv2 for security reasons; it wasn't disabled by default
            # until openssl 1.0.  The best way to do this is to use
            # the SSL_OP_NO_SSLv2, but that wasn't exposed to python
            # until 3.2.  Python 2.7 adds the ciphers argument, which
            # can also be used to disable SSLv2.  As a last resort
            # on python 2.6, we set ssl_version to TLSv1.  This is
            # more narrow than we'd like since it also breaks
            # compatibility with servers configured for SSLv3 only,
            # but nearly all servers support both SSLv3 and TLSv1:
            # http://blog.ivanristic.com/2011/09/ssl-survey-protocol-support.html
            if sys.version_info >= (2, 7):
                ssl_options["ciphers"] = "DEFAULT:!SSLv2"
            else:
                # This is really only necessary for pre-1.0 versions
                # of openssl, but python 2.6 doesn't expose version
                # information.
                ssl_options["ssl_version"] = ssl.PROTOCOL_TLSv1

            return SSLIOStream(socket.socket(af),
                               io_loop=self.io_loop,
                               ssl_options=ssl_options,
                               max_buffer_size=self.max_buffer_size)
        else:
            return IOStream(socket.socket(af),
                            io_loop=self.io_loop,
                            max_buffer_size=self.max_buffer_size)

    def _on_timeout(self):
        self._timeout = None
        if self.final_callback is not None:
            raise HTTPError(599, "Timeout")

    def _remove_timeout(self):
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None

    def _on_connect(self):
        self._remove_timeout()
        if self.final_callback is None:
            return
        if self.request.request_timeout:
            self._timeout = self.io_loop.add_timeout(
                self.start_time + self.request.request_timeout,
                stack_context.wrap(self._on_timeout))
        if (self.request.method not in self._SUPPORTED_METHODS and
                not self.request.allow_nonstandard_methods):
            raise KeyError("unknown method %s" % self.request.method)
        for key in ('network_interface',
                    'proxy_host', 'proxy_port',
                    'proxy_username', 'proxy_password'):
            if getattr(self.request, key, None):
                raise NotImplementedError('%s not supported' % key)
        if "Connection" not in self.request.headers:
            self.request.headers["Connection"] = "close"
        if "Host" not in self.request.headers:
            if '@' in self.parsed.netloc:
                self.request.headers["Host"] = self.parsed.netloc.rpartition('@')[-1]
            else:
                self.request.headers["Host"] = self.parsed.netloc
        username, password = None, None
        if self.parsed.username is not None:
            username, password = self.parsed.username, self.parsed.password
        elif self.request.auth_username is not None:
            username = self.request.auth_username
            password = self.request.auth_password or ''
        if username is not None:
            if self.request.auth_mode not in (None, "basic"):
                raise ValueError("unsupported auth_mode %s",
                                 self.request.auth_mode)
            auth = utf8(username) + b":" + utf8(password)
            self.request.headers["Authorization"] = (b"Basic " +
                                                     base64.b64encode(auth))
        if self.request.user_agent:
            self.request.headers["User-Agent"] = self.request.user_agent
        if not self.request.allow_nonstandard_methods:
            if self.request.method in ("POST", "PATCH", "PUT"):
                if (self.request.body is None and
                    self.request.body_producer is None):
                    raise AssertionError(
                        'Body must not be empty for "%s" request'
                        % self.request.method)
            else:
                if (self.request.body is not None or
                    self.request.body_producer is not None):
                    raise AssertionError(
                        'Body must be empty for "%s" request'
                        % self.request.method)
        if self.request.body is not None:
            # When body_producer is used the caller is responsible for
            # setting Content-Length (or else chunked encoding will be used).
            self.request.headers["Content-Length"] = str(len(
                self.request.body))
        if (self.request.method == "POST" and
                "Content-Type" not in self.request.headers):
            self.request.headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.request.use_gzip:
            self.request.headers["Accept-Encoding"] = "gzip"
        req_path = ((self.parsed.path or '/') +
                   (('?' + self.parsed.query) if self.parsed.query else ''))
        self.stream.set_nodelay(True)
        self.connection = HTTP1Connection(
            self.stream, self._sockaddr, is_client=True,
            no_keep_alive=True, protocol=self.parsed.scheme,
            max_header_size=self.max_header_size)
        start_line = httputil.RequestStartLine(self.request.method,
                                               req_path, 'HTTP/1.1')
        self.connection.write_headers(
            start_line, self.request.headers,
            has_body=(self.request.body is not None or
                      self.request.body_producer is not None))
        if self.request.body is not None:
            self.connection.write(self.request.body)
            self.connection.finish()
        elif self.request.body_producer is not None:
            fut = self.request.body_producer(self.connection.write)
            if is_future(fut):
                def on_body_written(fut):
                    fut.result()
                    self.connection.finish()
                    self._read_response()
                self.io_loop.add_future(fut, on_body_written)
                return
            self.connection.finish()
        self._read_response()

    def _read_response(self):
        # Ensure that any exception raised in read_response ends up in our
        # stack context.
        self.io_loop.add_future(
            self.connection.read_response(self, method=self.request.method,
                                          use_gzip=self.request.use_gzip),
            lambda f: f.result())

    def _release(self):
        if self.release_callback is not None:
            release_callback = self.release_callback
            self.release_callback = None
            release_callback()

    def _run_callback(self, response):
        self._release()
        if self.final_callback is not None:
            final_callback = self.final_callback
            self.final_callback = None
            self.io_loop.add_callback(final_callback, response)

    def _handle_exception(self, typ, value, tb):
        if self.final_callback:
            self._remove_timeout()
            if isinstance(value, StreamClosedError):
                value = HTTPError(599, "Stream closed")
            self._run_callback(HTTPResponse(self.request, 599, error=value,
                                            request_time=self.io_loop.time() - self.start_time,
                                            ))

            if hasattr(self, "stream"):
                # TODO: this may cause a StreamClosedError to be raised
                # by the connection's Future.  Should we cancel the
                # connection more gracefully?
                self.stream.close()
            return True
        else:
            # If our callback has already been called, we are probably
            # catching an exception that is not caused by us but rather
            # some child of our callback. Rather than drop it on the floor,
            # pass it along, unless it's just the stream being closed.
            return isinstance(value, StreamClosedError)

    def _on_close(self):
        if self.final_callback is not None:
            message = "Connection closed"
            if self.stream.error:
                message = str(self.stream.error)
            raise HTTPError(599, message)

    def headers_received(self, first_line, headers):
        self.headers = headers
        self.code = first_line.code
        self.reason = first_line.reason

        if "Content-Length" in self.headers:
            if "," in self.headers["Content-Length"]:
                # Proxies sometimes cause Content-Length headers to get
                # duplicated.  If all the values are identical then we can
                # use them but if they differ it's an error.
                pieces = re.split(r',\s*', self.headers["Content-Length"])
                if any(i != pieces[0] for i in pieces):
                    raise ValueError("Multiple unequal Content-Lengths: %r" %
                                     self.headers["Content-Length"])
                self.headers["Content-Length"] = pieces[0]
            content_length = int(self.headers["Content-Length"])
        else:
            content_length = None

        if self.request.header_callback is not None:
            # Reassemble the start line.
            self.request.header_callback('%s %s %s\r\n' % first_line)
            for k, v in self.headers.get_all():
                self.request.header_callback("%s: %s\r\n" % (k, v))
            self.request.header_callback('\r\n')

        if 100 <= self.code < 200 or self.code == 204:
            # These response codes never have bodies
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.3
            if ("Transfer-Encoding" in self.headers or
                    content_length not in (None, 0)):
                raise ValueError("Response with code %d should not have body" %
                                 self.code)

    def finish(self):
        data = b''.join(self.chunks)
        self._remove_timeout()
        original_request = getattr(self.request, "original_request",
                                   self.request)
        if (self.request.follow_redirects and
            self.request.max_redirects > 0 and
                self.code in (301, 302, 303, 307)):
            assert isinstance(self.request, _RequestProxy)
            new_request = copy.copy(self.request.request)
            new_request.url = urlparse.urljoin(self.request.url,
                                               self.headers["Location"])
            new_request.max_redirects = self.request.max_redirects - 1
            del new_request.headers["Host"]
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html#sec10.3.4
            # Client SHOULD make a GET request after a 303.
            # According to the spec, 302 should be followed by the same
            # method as the original request, but in practice browsers
            # treat 302 the same as 303, and many servers use 302 for
            # compatibility with pre-HTTP/1.1 user agents which don't
            # understand the 303 status.
            if self.code in (302, 303):
                new_request.method = "GET"
                new_request.body = None
                for h in ["Content-Length", "Content-Type",
                          "Content-Encoding", "Transfer-Encoding"]:
                    try:
                        del self.request.headers[h]
                    except KeyError:
                        pass
            new_request.original_request = original_request
            final_callback = self.final_callback
            self.final_callback = None
            self._release()
            self.client.fetch(new_request, final_callback)
            self._on_end_request()
            return
        if self.request.streaming_callback:
            buffer = BytesIO()
        else:
            buffer = BytesIO(data)  # TODO: don't require one big string?
        response = HTTPResponse(original_request,
                                self.code, reason=getattr(self, 'reason', None),
                                headers=self.headers,
                                request_time=self.io_loop.time() - self.start_time,
                                buffer=buffer,
                                effective_url=self.request.url)
        self._run_callback(response)
        self._on_end_request()

    def _on_end_request(self):
        self.stream.close()

    def data_received(self, chunk):
        if self.request.streaming_callback is not None:
            self.request.streaming_callback(chunk)
        else:
            self.chunks.append(chunk)


if __name__ == "__main__":
    AsyncHTTPClient.configure(SimpleAsyncHTTPClient)
    main()

import asyncio
import contextlib
import functools
import os
import traceback

from codeop import CommandCompiler
from io import BytesIO, StringIO


class StatefulCommandCompiler(CommandCompiler):
    def __init__(self):
        super().__init__()
        self.buf = BytesIO()

    def is_partial_command(self):
        return bool(self.buf.getvalue())

    def __call__(self, source, **kwargs):
        self.buf.write(source)

        code = self.buf.getvalue().decode('utf8')
        cleaned_code = code.replace('\r', '')

        # this is disgusting. Surely there must be a better way of handling
        # multiline functions.
        if (cleaned_code.startswith(('class ', 'def ')) and
                not cleaned_code.endswith('\n\n')):
            return

        codeobj = super().__call__(code, **kwargs)

        if codeobj:
            self.reset()
        return codeobj

    def reset(self):
        self.buf.seek(0)
        self.buf.truncate(0)


class InteractiveInterpreter:
    def __init__(self, namespace, banner):
        self.namespace = namespace
        self.banner = banner if isinstance(banner, bytes) else banner.encode('utf8')
        self.compiler = StatefulCommandCompiler()

    def attempt_compile(self, line):
        try:
            codeobj = self.compiler(line, symbol='eval')
        except SyntaxError:
            codeobj = self.compiler(b'')
        return codeobj

    def send_exception(self):
        self.compiler.reset()

        exc = traceback.format_exc()
        self.writer.write(exc.encode('utf8'))

        yield from self.writer.drain()

    def attempt_exec(self, codeobj, namespace):
        exc = None
        with contextlib.redirect_stdout(StringIO()) as buf:
            value = eval(codeobj, namespace)

        return value, buf.getvalue()

    @asyncio.coroutine
    def handle_one_command(self, namespace):
        reader = self.reader
        writer = self.writer

        while True:
            if self.compiler.is_partial_command():
                writer.write(b'... ')
            else:
                writer.write(b'>>> ')

            yield from writer.drain()

            line = yield from reader.readline()
            if line == b'':  # lost connection
                break

            try:
                codeobj = self.attempt_compile(line)
            except SyntaxError:
                self.send_exception()
                continue

            if codeobj is None:
                continue
            else:
                try:
                    value, stdout = self.attempt_exec(codeobj, namespace)
                except Exception:
                    yield from self.send_exception()
                    continue
                else:
                    if value is not None:
                        writer.write('{!r}\n'.format(value).encode('utf8'))
                        yield from writer.drain()
                    namespace['_'] = value

                if stdout:
                    writer.write(stdout.encode('utf8'))
                    yield from writer.drain()

    @asyncio.coroutine
    def __call__(self, reader, writer):
        self.reader = reader
        self.writer = writer

        if self.banner:
            writer.write(self.banner)
            yield from writer.drain()

        # one namespace per client, in case whole teams decide to congregate in
        # a single process.
        namespace = dict(self.namespace)

        while True:
            try:
                yield from self.handle_one_command(namespace)
            except ConnectionResetError:
                break
            except Exception as e:
                import traceback
                traceback.print_exc()


def start_manhole(banner=None, host='127.0.0.1', port=None, path=None, namespace=None):
    if (port, path) == (None, None):
        raise ValueError('At least one of port or path must be given')

    client_cb = InteractiveInterpreter(namespace or {}, banner)

    if path:
        f = asyncio.async(asyncio.start_unix_server(client_cb, path=path))

        @f.add_done_callback
        def done(task):
            def remove_manhole():
                try:
                    os.unlink(path)
                except OSError:
                    pass

            if task.exception() is None:
                import atexit
                atexit.register(remove_manhole)

    if port:
        asyncio.async(asyncio.start_server(client_cb, host=host, port=port))


if __name__ == '__main__':
    start_manhole(path='/var/tmp/testing.manhole', banner='Well this is neat\n')
    asyncio.get_event_loop().run_forever()

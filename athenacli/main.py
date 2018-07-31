# -*- coding: utf-8 -*-
import os
import sys
import click
import threading
import logging
import itertools
import sqlparse
import traceback
from time import time
from datetime import datetime
from random import choice
from collections import namedtuple

from prompt_toolkit.layout.prompt import DefaultPrompt
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import create_prompt_layout, create_eventloop
from prompt_toolkit.document import Document
from prompt_toolkit.layout.processors import (
    HighlightMatchingBracketProcessor,
    ConditionalProcessor)
from prompt_toolkit.filters import Always, HasFocus, IsDone
from prompt_toolkit.enums import DEFAULT_BUFFER, EditingMode
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.interface import AcceptAction
from prompt_toolkit import CommandLineInterface, Application, AbortAction
from prompt_toolkit import CommandLineInterface, Application, AbortAction
from prompt_toolkit.styles.from_pygments import style_from_pygments
from pygments.lexers.sql import SqlLexer
from pygments.token import Token
from cli_helpers.tabular_output import TabularOutputFormatter
from cli_helpers.tabular_output import preprocessors
from pyathena.error import OperationalError

import athenacli.packages.special as special
from athenacli.sqlexecute import SQLExecute
from athenacli.completer import AthenaCompleter
from athenacli.style import AthenaStyle
from athenacli.completion_refresher import CompletionRefresher
from athenacli.packages.tabular_output import sql_format
from athenacli.clistyle import style_factory
from athenacli.packages.prompt_utils import confirm, confirm_destructive_query
from athenacli.key_bindings import cli_bindings
from athenacli.clitoolbar import create_toolbar_tokens_func
from athenacli.lexer import Lexer
from athenacli.clibuffer import CLIBuffer
from athenacli.sqlexecute import SQLExecute
from athenacli.encodingutils import utf8tounicode, text_type
from athenacli.config import read_config_files, write_default_config, mkdir_p


# Query tuples are used for maintaining history
Query = namedtuple('Query', ['query', 'successful', 'mutating'])

LOGGER = logging.getLogger(__name__)
PACKAGE_ROOT = os.path.abspath(os.path.dirname(__file__))
ATHENACLIRC = '~/.athenacli/athenaclirc'
DEFAULT_CONFIG_FILE = os.path.join(PACKAGE_ROOT, 'athenaclirc')



class AthenaCli(object):
    DEFAULT_PROMPT = '\\d@\\r> '
    MAX_LEN_PROMPT = 45

    def __init__(self, region=None, database=None):
        config_files = (DEFAULT_CONFIG_FILE, ATHENACLIRC)
        _cfg = self.config = read_config_files(config_files)

        self.init_logging(_cfg['main']['log_file'], _cfg['main']['log_level'])

        self.connect(region, database)

        self.multi_line = _cfg['main'].as_bool('multi_line')
        self.key_bindings = _cfg['main']['key_bindings']
        self.prompt = _cfg['main']['prompt'] or self.DEFAULT_PROMPT

        self.formatter = TabularOutputFormatter(_cfg['main']['table_format'])
        self.formatter.cli = self
        sql_format.register_new_formatter(self.formatter)

        self.output_style = style_factory('default', {})

        self.completer = AthenaCompleter()
        self._completer_lock = threading.Lock()
        self.completion_refresher = CompletionRefresher()

        self.cli = None

        # Register custom special commands.
        self.register_special_commands()

    def init_logging(self, log_file, log_level_str):
        file_path = os.path.expanduser(log_file)
        if not os.path.exists(file_path):
            mkdir_p(os.path.dirname(file_path))

        handler = logging.FileHandler(os.path.expanduser(log_file))
        log_level_map = {
            'CRITICAL': logging.CRITICAL,
            'ERROR': logging.ERROR,
            'WARNING': logging.WARNING,
            'INFO': logging.INFO,
            'DEBUG': logging.DEBUG,
        }

        log_level = log_level_map[log_level_str.upper()]

        formatter = logging.Formatter(
            '%(asctime)s (%(process)d/%(threadName)s) '
            '%(name)s %(levelname)s - %(message)s')

        handler.setFormatter(formatter)

        LOGGER.addHandler(handler)
        LOGGER.setLevel(log_level)

        root_logger = logging.getLogger('athenacli')
        root_logger.addHandler(handler)
        root_logger.setLevel(log_level)

        root_logger.debug('Initializing athenacli logging.')
        root_logger.debug('Log file %r.', log_file)

        pgspecial_logger = logging.getLogger('special')
        pgspecial_logger.addHandler(handler)
        pgspecial_logger.setLevel(log_level)

    def register_special_commands(self):
        special.register_special_command(self.change_db, 'use',
                '\\u', 'Change to a new database.', aliases=('\\u',))
        special.register_special_command(self.change_prompt_format, 'prompt',
                '\\R', 'Change prompt format.', aliases=('\\R',), case_sensitive=True)

    def change_db(self, arg, **_):
        if arg is None:
            self.sqlexecute.connect()
        else:
            self.sqlexecute.connect(database=arg)

        yield (None, None, None, 'You are now connected to database "%s"' % self.sqlexecute.database)

    def change_prompt_format(self, arg, **_):
        """
        Change the prompt format.
        """
        if not arg:
            message = 'Missing required argument, format.'
            return [(None, None, None, message)]

        self.prompt = self.get_prompt(arg)
        return [(None, None, None, "Changed prompt format to %s" % arg)]

    def connect(self, region, database):
        _cfg = self.config['main']
        self.sqlexecute = SQLExecute(
            _cfg['aws_access_key_id'],
            _cfg['aws_secret_access_key'],
            region or _cfg['region_name'],
            _cfg['s3_staging_dir'],
            database or _cfg['schema_name']
        )

    def get_aws_key(self, material_set):
        'Return the (access_key, secret_access_key) pair for a odin material set.'


    def run_cli(self):
        self.iterations = 0
        self.refresh_completions()

        history_file = os.path.expanduser(self.config['main']['history_file'])
        history = FileHistory(history_file)
        self.cli = self._build_cli(history)

        def one_iteration():
            document = self.cli.run()
            if not document.text.strip():
                return

            special.set_expanded_output(False)
            mutating = False

            try:
                LOGGER.debug('sql: %r', document.text)

                special.write_tee(self.get_prompt(self.prompt) + document.text)
                successful = False
                start = time()
                res = self.sqlexecute.run(document.text)
                successful = True
                threshold = 1000
                result_count = 0

                for title, cur, headers, status in res:
                    if (is_select(status)
                        and cur and cur.rowcount > threshold):
                        self.echo(
                            'The result set has more than {} rows.'.format(threshold),
                            fg='red'
                        )
                        if not confirm('Do you want to continue?'):
                            self.echo('Aborted!', err=True, fg='red')
                            break

                    formatted = self.format_output(
                        title, cur, headers, special.is_expanded_output(), None
                    )

                    t = time() - start
                    try:
                        if result_count > 0:
                            self.echo('')
                        try:
                            self.output(formatted, status)
                        except KeyboardInterrupt:
                            pass

                        if special.is_timing_enabled():
                            self.echo('Time: %0.03fs' % t)
                    except KeyboardInterrupt:
                        pass

                    start = time()
                    result_count += 1
                    mutating = mutating or is_mutating(status)
                special.unset_once_if_written()
            except EOFError as e:
                raise e
            except KeyboardInterrupt:
                pass
            except NotImplementedError:
                self.echo('Not Yet Implemented.', fg="yellow")
            except OperationalError as e:
                LOGGER.debug("Exception: %r", e)
                LOGGER.error("sql: %r, error: %r", document.text, e)
                LOGGER.error("traceback: %r", traceback.format_exc())
                self.echo(str(e), err=True, fg='red')
            except Exception as e:
                LOGGER.error("sql: %r, error: %r", document.text, e)
                LOGGER.error("traceback: %r", traceback.format_exc())
                self.echo(str(e), err=True, fg='red')
            else:
                # Refresh the table names and column names if necessary.
                if need_completion_refresh(document.text):
                    LOGGER.debug("=" * 10)
                    self.refresh_completions()

            query = Query(document.text, successful, mutating)

        try:
            while True:
                one_iteration()
                self.iterations += 1
        except EOFError:
            special.close_tee()

    def get_output_margin(self, status=None):
        """Get the output margin (number of rows for the prompt, footer and
        timing message."""
        margin = self.get_reserved_space() + self.get_prompt(self.prompt).count('\n') + 1
        if special.is_timing_enabled():
            margin += 1
        if status:
            margin += 1 + status.count('\n')

        return margin

    def output(self, output, status=None):
        """Output text to stdout or a pager command.
        The status text is not outputted to pager or files.
        The message will be logged in the audit log, if enabled. The
        message will be written to the tee file, if enabled. The
        message will be written to the output file, if enabled.
        """
        if output:
            size = self.cli.output.get_size()

            margin = self.get_output_margin(status)

            fits = True
            buf = []
            output_via_pager = False
            for i, line in enumerate(output, 1):
                special.write_tee(line)
                special.write_once(line)

                if fits or output_via_pager:
                    # buffering
                    buf.append(line)
                    if len(line) > size.columns or i > (size.rows - margin):
                        fits = False
                        if not output_via_pager:
                            # doesn't fit, flush buffer
                            for line in buf:
                                click.secho(line)
                            buf = []
                else:
                    click.secho(line)

            if buf:
                if output_via_pager:
                    # sadly click.echo_via_pager doesn't accept generators
                    click.echo_via_pager("\n".join(buf))
                else:
                    for line in buf:
                        click.secho(line)

        if status:
            click.secho(status)

    def format_output(self, title, cur, headers, expanded=False,
                      max_width=None):
        expanded = expanded or self.formatter.format_name == 'vertical'
        output = []

        output_kwargs = {
            'disable_numparse': True,
            'preserve_whitespace': True,
            'preprocessors': (preprocessors.align_decimals, ),
            'style': self.output_style
        }

        if title:  # Only print the title if it's not None.
            output = itertools.chain(output, [title])

        if cur:
            column_types = None
            if hasattr(cur, 'description'):
                def get_col_type(col):
                    col_type = text_type
                    return col_type if type(col_type) is type else text_type
                column_types = [get_col_type(col) for col in cur.description]

            if max_width is not None:
                cur = list(cur)

            formatted = self.formatter.format_output(
                cur, headers, format_name='vertical' if expanded else None,
                column_types=column_types,
                **output_kwargs)

            if isinstance(formatted, (text_type)):
                formatted = formatted.splitlines()
            formatted = iter(formatted)

            first_line = next(formatted)
            formatted = itertools.chain([first_line], formatted)

            if (not expanded and max_width and headers and cur and
                    len(first_line) > max_width):
                formatted = self.formatter.format_output(
                    cur, headers, format_name='vertical', column_types=column_types, **output_kwargs)
                if isinstance(formatted, (text_type)):
                    formatted = iter(formatted.splitlines())

            output = itertools.chain(output, formatted)

        return output

    def echo(self, s, **kwargs):
        """Print a message to stdout.
        The message will be logged in the audit log, if enabled.
        All keyword arguments are passed to click.echo().
        """
        click.secho(s, **kwargs)

    def refresh_completions(self):
        with self._completer_lock:
            self.completer.reset_completions()

        completer_options = {
            'smart_completion': True,
            'supported_formats': self.formatter.supported_formats,
            'keyword_casing': self.completer.keyword_casing
        }
        self.completion_refresher.refresh(
            self.sqlexecute,
            self._on_completions_refreshed,
            completer_options
        )

    def _on_completions_refreshed(self, new_completer):
        """Swap the completer object in cli with the newly created completer.
        """
        with self._completer_lock:
            self.completer = new_completer
            # When cli is first launched we call refresh_completions before
            # instantiating the cli object. So it is necessary to check if cli
            # exists before trying the replace the completer object in cli.
            if self.cli:
                self.cli.current_buffer.completer = new_completer

        if self.cli:
            # After refreshing, redraw the CLI to clear the statusbar
            # "Refreshing completions..." indicator
            self.cli.request_redraw()

    def _build_cli(self, history):
        key_binding_manager = cli_bindings()

        def prompt_tokens(cli):
            prompt = self.get_prompt(self.prompt)
            if len(prompt) > self.MAX_LEN_PROMPT:
                prompt = self.get_prompt('\\r:\\d> ')
            return [(Token.Prompt, prompt)]

        def get_continuation_tokens(cli, width):
            prompt = self.get_prompt('|>')
            token = (
                Token.Continuation,
                ' ' * (width - len(prompt)) + prompt
            )
            return [token]

        def show_suggestion_tip():
            return self.iterations < 2

        get_toolbar_tokens = create_toolbar_tokens_func(
            self.completion_refresher.is_refreshing,
            show_suggestion_tip)

        layout = create_prompt_layout(
            lexer=Lexer,
            multiline=True,
            get_prompt_tokens=prompt_tokens,
            get_continuation_tokens=get_continuation_tokens,
            get_bottom_toolbar_tokens=get_toolbar_tokens,
            display_completions_in_columns=False,
            extra_input_processors=[
                ConditionalProcessor(
                    processor=HighlightMatchingBracketProcessor(chars='[](){}'),
                    filter=HasFocus(DEFAULT_BUFFER) & ~IsDone())
            ],
            reserve_space_for_menu=self.get_reserved_space()
        )

        with self._completer_lock:
            buf = CLIBuffer(
                always_multiline=self.multi_line,
                completer=self.completer,
                history=history,
                auto_suggest=AutoSuggestFromHistory(),
                complete_while_typing=Always(),
                accept_action=AcceptAction.RETURN_DOCUMENT)

            if self.key_bindings == 'vi':
                editing_mode = EditingMode.VI
            else:
                editing_mode = EditingMode.EMACS

            application = Application(
                style=style_from_pygments(style_cls=self.output_style),
                layout=layout,
                buffer=buf,
                key_bindings_registry=key_binding_manager.registry,
                on_exit=AbortAction.RAISE_EXCEPTION,
                on_abort=AbortAction.RETRY,
                editing_mode=editing_mode,
                ignore_case=True)

            cli = CommandLineInterface(
                application=application,
                eventloop=create_eventloop())

            return cli

    def get_prompt(self, string):
        sqlexecute = self.sqlexecute
        LOGGER.debug("aaaaaaaaaa %s" % string)
        LOGGER.debug(sqlexecute.database)

        string = string.replace('\\r', sqlexecute.region_name or '(none)')
        string = string.replace('\\d', sqlexecute.database or '(none)')
        return string

    def get_reserved_space(self):
        """Get the number of lines to reserve for the completion menu."""
        reserved_space_ratio = .45
        max_reserved_space = 8
        _, height = click.get_terminal_size()
        return min(int(round(height * reserved_space_ratio)), max_reserved_space)


def need_completion_refresh(queries):
    """Determines if the completion needs a refresh by checking if the sql
    statement is an alter, create, drop or change db."""
    for query in sqlparse.split(queries):
        try:
            first_token = query.split()[0]
            if first_token.lower() in ('use', '\\u'):
                return True
        except Exception:
            return False


def is_mutating(status):
    """Determines if the statement is mutating based on the status."""
    if not status:
        return False

    mutating = set(['insert', 'update', 'delete', 'alter', 'create', 'drop',
                    'replace', 'truncate', 'load'])
    return status.split(None, 1)[0].lower() in mutating

def is_select(status):
    """Returns true if the first word in status is 'select'."""
    if not status:
        return False
    return status.split(None, 1)[0].lower() == 'select'


@click.command()
@click.argument('database', default='', nargs=1)
def cli(database):
    '''A Athena terminal client with auto-completion and syntax highlighting.

    \b
    Examples:
      - athenacli
      - athenacli my_database
    '''
    if not os.path.exists(os.path.expanduser(ATHENACLIRC)):
        err_msg = '''
        Welcome to athenacli!

        It seems this is your first time to run athenacli,
        we generated a default config file for you
            %s
        Please change it accordingly, and run athenacli again.
        ''' % ATHENACLIRC
        print(err_msg)
        write_default_config(DEFAULT_CONFIG_FILE, ATHENACLIRC)
        sys.exit(1)

    print("========******" * 10)

    athenacli = AthenaCli(database=database)
    athenacli.run_cli()


if __name__ == '__main__':
    cli()
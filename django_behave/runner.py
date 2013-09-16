"""Django test runner which uses behave for BDD tests.
"""

import unittest
from os.path import dirname, abspath, basename, join, isdir
import sys
import new

from django.core.management.color import no_style
from django.db import connections, transaction, DEFAULT_DB_ALIAS
from django.db.models.loading import cache
from django.test.simple import DjangoTestSuiteRunner, reorder_suite
from django.test import LiveServerTestCase
from django.db.models import get_app
from django.core.management.commands.loaddata import Command
from behave.configuration import Configuration, ConfigError
from behave.runner import Runner, os
from behave.parser import ParserError
from behave.formatter.ansi_escapes import escapes


def get_app_dir(app_module):
    app_dir = dirname(app_module.__file__)
    if basename(app_dir) == 'models':
        app_dir = abspath(join(app_dir, '..'))
    return app_dir


def get_features(app_module):
    app_dir = get_app_dir(app_module)
    features_dir = abspath(join(app_dir, 'features'))
    if isdir(features_dir):
        return features_dir
    else:
        return None


class DjangoBehaveTestCase(LiveServerTestCase):
    def __init__(self, **kwargs):
        self.features_dir = kwargs.pop('features_dir')
        super(DjangoBehaveTestCase, self).__init__(**kwargs)
        unittest.TestCase.__init__(self)

    def _fixture_setup(self):
        if _reusing_db():
            return
        super(DjangoBehaveTestCase, self)._fixture_setup()

    def get_features_dir(self):
        if isinstance(self.features_dir, basestring):
            return [self.features_dir]
        return self.features_dir

    def setUp(self):
        self.setupBehave()

    def setupBehave(self):
        # sys.argv kludge
        # need to understand how to do this better
        # temporarily lose all the options etc
        # else behave will complain
        old_argv = sys.argv
        sys.argv = old_argv[:2]
        self.behave_config = Configuration()
        sys.argv = old_argv
        # end of sys.argv kludge

        self.behave_config.server_url = self.live_server_url  # property of LiveServerTestCase
        self.behave_config.paths = self.get_features_dir()
        self.behave_config.format = ['pretty']
        # disable these in case you want to add set_trace in the tests you're developing
        self.behave_config.stdout_capture = False
        self.behave_config.stderr_capture = False

    def runTest(self, result=None):
        # run behave on a single directory

        # from behave/__main__.py
        #stream = self.behave_config.output
        runner = Runner(self.behave_config)
        try:
            failed = runner.run()
        except ParserError, e:
            sys.exit(str(e))
        except ConfigError, e:
            sys.exit(str(e))

        if self.behave_config.show_snippets and runner.undefined:
            msg = u"\nYou can implement step definitions for undefined steps with "
            msg += u"these snippets:\n\n"
            printed = set()

            if sys.version_info[0] == 3:
                string_prefix = "('"
            else:
                string_prefix = u"(u'"

            for step in set(runner.undefined):
                if step in printed:
                    continue
                printed.add(step)

                msg += u"@" + step.step_type + string_prefix + step.name + u"')\n"
                msg += u"def impl(context):\n"
                msg += u"    assert False\n\n"

            sys.stderr.write(escapes['undefined'] + msg + escapes['reset'])
            sys.stderr.flush()

        if failed:
            sys.exit(1)
        # end of from behave/__main__.py


def make_test_suite(test_labels, **kwargs):
    test_suite = DjangoTestSuiteRunner()
    return test_suite.build_suite(test_labels, **kwargs)


class DjangoBehaveTestSuiteRunner(DjangoTestSuiteRunner):
    def make_bdd_test_suite(self, features_dir):
        return DjangoBehaveTestCase(features_dir=features_dir)

    def build_suite(self, test_labels, extra_tests=None, **kwargs):
        # build standard Django test suite
        suite = unittest.TestSuite()

        #
        # Run Normal Django Test Suite
        #
        std_test_suite = make_test_suite(test_labels, **kwargs)
        suite.addTest(std_test_suite)

        #
        # Add BDD tests to it
        #

        # always get all features for given apps (for convenience)
        for label in test_labels:
            if '.' in label:
                print "Ignoring label with dot in: " % label
                continue
            app = get_app(label)

            # Check to see if a separate 'features' module exists,
            # parallel to the models module
            features_dir = get_features(app)
            if features_dir is not None:
                # build a test suite for this directory
                suite.addTest(self.make_bdd_test_suite(features_dir))

        return reorder_suite(suite, (LiveServerTestCase,))


##
## Runner which reuses test db if already created
## taken from django-nose
## see: https://github.com/jbalogh/django-nose/blob/master/django_nose/runner.py
##


def uses_mysql(connection):
    """Return whether the connection represents a MySQL DB."""
    return 'mysql' in connection.settings_dict['ENGINE']


_old_handle = Command.handle


def _foreign_key_ignoring_handle(self, *fixture_labels, **options):
    """Wrap the the stock loaddata to ignore foreign key
    checks so we can load circular references from fixtures.

    This is monkeypatched into place in setup_databases().

    """
    using = options.get('database', DEFAULT_DB_ALIAS)
    commit = options.get('commit', True)
    connection = connections[using]

    # MySQL stinks at loading circular references:
    if uses_mysql(connection):
        cursor = connection.cursor()
        cursor.execute('SET foreign_key_checks = 0')

    _old_handle(self, *fixture_labels, **options)

    if uses_mysql(connection):
        cursor = connection.cursor()
        cursor.execute('SET foreign_key_checks = 1')

        if commit:
            connection.close()


def _skip_create_test_db(self, verbosity=1, autoclobber=False):
    """``create_test_db`` implementation that skips both creation and flushing

    The idea is to re-use the perfectly good test DB already created by an
    earlier test run, cutting the time spent before any tests run from 5-13s
    (depending on your I/O luck) down to 3.

    """
    # Notice that the DB supports transactions. Originally, this was done in
    # the method this overrides. The confirm method was added in Django v1.3
    # (https://code.djangoproject.com/ticket/12991) but removed in Django v1.5
    # (https://code.djangoproject.com/ticket/17760). In Django v1.5
    # supports_transactions is a cached property evaluated on access.
    if callable(getattr(self.connection.features, 'confirm', None)):
        # Django v1.3-4
        self.connection.features.confirm()
    elif hasattr(self, "_rollback_works"):
        # Django v1.2 and lower
        can_rollback = self._rollback_works()
        self.connection.settings_dict['SUPPORTS_TRANSACTIONS'] = can_rollback

    return self._get_test_db_name()


def _reusing_db():
    """Return whether the ``REUSE_DB`` flag was passed"""
    return os.getenv('REUSE_DB', 'false').lower() in ('true', '1', '')


def _can_support_reuse_db(connection):
    """Return whether it makes any sense to
    use REUSE_DB with the backend of a connection."""
    # Perhaps this is a SQLite in-memory DB. Those are created implicitly when
    # you try to connect to them, so our usual test doesn't work.
    return not connection.creation._get_test_db_name() == ':memory:'


def _should_create_database(connection):
    """Return whether we should recreate the given DB.

    This is true if the DB doesn't exist or the REUSE_DB env var isn't truthy.

    """
    # TODO: Notice when the Model classes change and return True. Worst case,
    # we can generate sqlall and hash it, though it's a bit slow (2 secs) and
    # hits the DB for no good reason. Until we find a faster way, I'm inclined
    # to keep making people explicitly saying REUSE_DB if they want to reuse
    # the DB.

    if not _can_support_reuse_db(connection):
        return True

    # Notice whether the DB exists, and create it if it doesn't:
    try:
        connection.cursor()
    except Exception:  # TODO: Be more discerning but still DB agnostic.
        return True
    return not _reusing_db()


def _mysql_reset_sequences(style, connection):
    """Return a list of SQL statements needed to
    reset all sequences for Django tables."""
    tables = connection.introspection.django_table_names(only_existing=True)
    flush_statements = connection.ops.sql_flush(
            style, tables, connection.introspection.sequence_list())

    # connection.ops.sequence_reset_sql() is not implemented for MySQL,
    # and the base class just returns []. TODO: Implement it by pulling
    # the relevant bits out of sql_flush().
    return [s for s in flush_statements if s.startswith('ALTER')]
    # Being overzealous and resetting the sequences on non-empty tables
    # like django_content_type seems to be fine in MySQL: adding a row
    # afterward does find the correct sequence number rather than
    # crashing into an existing row.


class DjangoBehaveReuseDbTestSuiteRunner(DjangoBehaveTestSuiteRunner):
    """A runner that optionally skips DB creation

    This test monkeypatches connection.creation to let you skip creating
    databases if they already exist. Your tests will run much faster.

    To opt into this behavior, set the environment variable ``REUSE_DB`` to
    something that isn't "0" or "false" (case aside).

    """
    def setup_databases(self):
        for alias in connections:
            connection = connections[alias]
            creation = connection.creation
            test_db_name = creation._get_test_db_name()

            # Mess with the DB name so other things operate on a test DB
            # rather than the real one. This is done in create_test_db when
            # we don't monkeypatch it away with _skip_create_test_db.
            orig_db_name = connection.settings_dict['NAME']
            connection.settings_dict['NAME'] = test_db_name

            if not _reusing_db() and _can_support_reuse_db(connection):
                print ('To reuse old database "%s" for speed, set env var '
                       'REUSE_DB=1.' % test_db_name)

            if _should_create_database(connection):
                # We're not using _skip_create_test_db, so put the DB name back:
                connection.settings_dict['NAME'] = orig_db_name

                # Since we replaced the connection with the test DB, closing
                # the connection will avoid pooling issues with SQLAlchemy. The
                # issue is trying to CREATE/DROP the test database using a
                # connection to a DB that was established with that test DB.
                # MySQLdb doesn't allow it, and SQLAlchemy attempts to reuse
                # the existing connection from its pool.
                connection.close()
            else:
                creation.create_test_db = new.instancemethod(_skip_create_test_db, creation, creation.__class__)

        Command.handle = _foreign_key_ignoring_handle

        # With our class patch, does nothing but return some connection
        # objects:
        return super(DjangoBehaveReuseDbTestSuiteRunner, self).setup_databases()

    def teardown_databases(self, *args, **kwargs):
        """Leave those poor, reusable databases alone if REUSE_DB is true."""
        if not _reusing_db():
            return super(DjangoBehaveReuseDbTestSuiteRunner, self).teardown_databases(*args, **kwargs)
        # else skip tearing down the DB so we can reuse it next time

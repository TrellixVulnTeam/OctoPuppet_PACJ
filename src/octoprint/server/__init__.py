# coding=utf-8
from __future__ import absolute_import, division, print_function

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import uuid
from sockjs.tornado import SockJSRouter
from flask import Flask, g, request, session, Blueprint
from flask.ext.login import LoginManager, current_user
from flask.ext.principal import Principal, Permission, RoleNeed, identity_loaded, UserNeed
from flask.ext.babel import Babel, gettext, ngettext
from flask.ext.assets import Environment, Bundle
from flaskext.markdown import Markdown
from babel import Locale
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from collections import defaultdict
from builtins import range

import os
import logging
import logging.config
import atexit
import signal
import base64

SUCCESS = {}
NO_CONTENT = ("", 204)
NOT_MODIFIED = ("Not Modified", 304)

app = Flask("octoprint")
assets = None
babel = None
debug = False

printer = None
printerProfileManager = None
fileManager = None
slicingManager = None
analysisQueue = None
userManager = None
eventManager = None
loginManager = None
pluginManager = None
appSessionManager = None
pluginLifecycleManager = None
preemptiveCache = None

principals = Principal(app)
admin_permission = Permission(RoleNeed("admin"))
user_permission = Permission(RoleNeed("user"))

# only import the octoprint stuff down here, as it might depend on things defined above to be initialized already
from octoprint import __version__, __branch__, __display_version__, __revision__
from octoprint.printer.profile import PrinterProfileManager
from octoprint.printer.standard import Printer
from octoprint.settings import settings
import octoprint.users as users
import octoprint.events as events
import octoprint.plugin
import octoprint.timelapse
import octoprint._version
import octoprint.util
import octoprint.filemanager.storage
import octoprint.filemanager.analysis
import octoprint.slicing
from octoprint.server.util.flask import PreemptiveCache

from . import util

UI_API_KEY = ''.join('%02X' % ord(z) for z in uuid.uuid4().bytes)

VERSION = __version__
BRANCH = __branch__
DISPLAY_VERSION = __display_version__
REVISION = __revision__

LOCALES = []
LANGUAGES = set()

@identity_loaded.connect_via(app)
def on_identity_loaded(sender, identity):
	user = load_user(identity.id)
	if user is None:
		return

	identity.provides.add(UserNeed(user.get_id()))
	if user.is_user():
		identity.provides.add(RoleNeed("user"))
	if user.is_admin():
		identity.provides.add(RoleNeed("admin"))

def load_user(id):
	if id == "_api":
		return users.ApiUser()

	if session and "usersession.id" in session:
		sessionid = session["usersession.id"]
	else:
		sessionid = None

	if userManager.enabled:
		if sessionid:
			return userManager.findUser(userid=id, session=sessionid)
		else:
			return userManager.findUser(userid=id)
	return users.DummyUser()


#~~ startup code


class Server(object):
	def __init__(self, settings=None, plugin_manager=None, host="0.0.0.0", port=5000, debug=False, allow_root=False, octoprint_daemon=None):
		self._settings = settings
		self._plugin_manager = plugin_manager
		self._host = host
		self._port = port
		self._debug = debug
		self._allow_root = allow_root
		self._octoprint_daemon = octoprint_daemon
		self._server = None

		self._logger = None

		self._lifecycle_callbacks = defaultdict(list)

		self._template_searchpaths = []

		self._intermediary_server = None

	def run(self):
		if not self._allow_root:
			self._check_for_root()

		if self._settings is None:
			self._settings = settings()
		if self._plugin_manager is None:
			self._plugin_manager = octoprint.plugin.plugin_manager()

		global app
		global babel

		global printer
		global printerProfileManager
		global fileManager
		global slicingManager
		global analysisQueue
		global userManager
		global eventManager
		global loginManager
		global pluginManager
		global appSessionManager
		global pluginLifecycleManager
		global preemptiveCache
		global debug

		from tornado.ioloop import IOLoop
		from tornado.web import Application, RequestHandler

		debug = self._debug

		self._logger = logging.getLogger(__name__)
		pluginManager = self._plugin_manager

		# monkey patch a bunch of stuff
		util.tornado.fix_ioloop_scheduling()
		util.flask.enable_additional_translations(additional_folders=[self._settings.getBaseFolder("translations")])

		# setup app
		self._setup_app()

		# setup i18n
		self._setup_i18n(app)

		if self._settings.getBoolean(["serial", "log"]):
			# enable debug logging to serial.log
			logging.getLogger("SERIAL").setLevel(logging.DEBUG)
			logging.getLogger("SERIAL").debug("Enabling serial logging")

		# start the intermediary server
		self._start_intermediary_server()

		# then initialize the plugin manager
		pluginManager.reload_plugins(startup=True, initialize_implementations=False)

		printerProfileManager = PrinterProfileManager()
		eventManager = events.eventManager()
		analysisQueue = octoprint.filemanager.analysis.AnalysisQueue()
		slicingManager = octoprint.slicing.SlicingManager(self._settings.getBaseFolder("slicingProfiles"), printerProfileManager)
		storage_managers = dict()
		storage_managers[octoprint.filemanager.FileDestinations.LOCAL] = octoprint.filemanager.storage.LocalFileStorage(self._settings.getBaseFolder("uploads"))
		fileManager = octoprint.filemanager.FileManager(analysisQueue, slicingManager, printerProfileManager, initial_storage_managers=storage_managers)
		appSessionManager = util.flask.AppSessionManager()
		pluginLifecycleManager = LifecycleManager(pluginManager)
		preemptiveCache = PreemptiveCache(os.path.join(self._settings.getBaseFolder("data"), "preemptive_cache_config.yaml"))

		# setup access control
		userManagerName = self._settings.get(["accessControl", "userManager"])
		try:
			clazz = octoprint.util.get_class(userManagerName)
			userManager = clazz()
		except AttributeError as e:
			self._logger.exception("Could not instantiate user manager {}, falling back to FilebasedUserManager!".format(userManagerName))
			userManager = octoprint.users.FilebasedUserManager()
		finally:
			userManager.enabled = self._settings.getBoolean(["accessControl", "enabled"])

		components = dict(
			plugin_manager=pluginManager,
			printer_profile_manager=printerProfileManager,
			event_bus=eventManager,
			analysis_queue=analysisQueue,
			slicing_manager=slicingManager,
			file_manager=fileManager,
			app_session_manager=appSessionManager,
			plugin_lifecycle_manager=pluginLifecycleManager,
			user_manager=userManager,
			preemptive_cache=preemptiveCache
		)

		# create printer instance
		printer_factories = pluginManager.get_hooks("octoprint.printer.factory")
		for name, factory in printer_factories.items():
			try:
				printer = factory(components)
				if printer is not None:
					self._logger.debug("Created printer instance from factory {}".format(name))
					break
			except:
				self._logger.exception("Error while creating printer instance from factory {}".format(name))
		else:
			printer = Printer(fileManager, analysisQueue, printerProfileManager)
		components.update(dict(printer=printer))

		def octoprint_plugin_inject_factory(name, implementation):
			if not isinstance(implementation, octoprint.plugin.OctoPrintPlugin):
				return None
			props = dict()
			props.update(components)
			props.update(dict(
				data_folder=os.path.join(self._settings.getBaseFolder("data"), name)
			))
			return props

		def settings_plugin_inject_factory(name, implementation):
			plugin_settings = octoprint.plugin.plugin_settings_for_settings_plugin(name, implementation)
			if plugin_settings is None:
				return

			return dict(settings=plugin_settings)

		def settings_plugin_config_migration_and_cleanup(name, implementation):
			if not isinstance(implementation, octoprint.plugin.SettingsPlugin):
				return

			settings_version = implementation.get_settings_version()
			settings_migrator = implementation.on_settings_migrate

			if settings_version is not None and settings_migrator is not None:
				stored_version = implementation._settings.get_int([octoprint.plugin.SettingsPlugin.config_version_key])
				if stored_version is None or stored_version < settings_version:
					settings_migrator(settings_version, stored_version)
					implementation._settings.set_int([octoprint.plugin.SettingsPlugin.config_version_key], settings_version)

			implementation.on_settings_cleanup()
			implementation._settings.save()

			implementation.on_settings_initialized()

		pluginManager.implementation_inject_factories=[octoprint_plugin_inject_factory, settings_plugin_inject_factory]
		pluginManager.initialize_implementations()

		settingsPlugins = pluginManager.get_implementations(octoprint.plugin.SettingsPlugin)
		for implementation in settingsPlugins:
			try:
				settings_plugin_config_migration_and_cleanup(implementation._identifier, implementation)
			except:
				self._logger.exception("Error while trying to migrate settings for plugin {}, ignoring it".format(implementation._identifier))

		pluginManager.implementation_post_inits=[settings_plugin_config_migration_and_cleanup]

		pluginManager.log_all_plugins()

		# initialize file manager and register it for changes in the registered plugins
		fileManager.initialize()
		pluginLifecycleManager.add_callback(["enabled", "disabled"], lambda name, plugin: fileManager.reload_plugins())

		# initialize slicing manager and register it for changes in the registered plugins
		slicingManager.initialize()
		pluginLifecycleManager.add_callback(["enabled", "disabled"], lambda name, plugin: slicingManager.reload_slicers())

		# setup jinja2
		self._setup_jinja2()
		def template_enabled(name, plugin):
			if plugin.implementation is None or not isinstance(plugin.implementation, octoprint.plugin.TemplatePlugin):
				return
			self._register_additional_template_plugin(plugin.implementation)
		def template_disabled(name, plugin):
			if plugin.implementation is None or not isinstance(plugin.implementation, octoprint.plugin.TemplatePlugin):
				return
			self._unregister_additional_template_plugin(plugin.implementation)
		pluginLifecycleManager.add_callback("enabled", template_enabled)
		pluginLifecycleManager.add_callback("disabled", template_disabled)

		# setup assets
		self._setup_assets()

		# configure timelapse
		octoprint.timelapse.configure_timelapse()

		# setup command triggers
		events.CommandTrigger(printer)
		if self._debug:
			events.DebugEventListener()

		app.wsgi_app = util.ReverseProxied(
			app.wsgi_app,
			self._settings.get(["server", "reverseProxy", "prefixHeader"]),
			self._settings.get(["server", "reverseProxy", "schemeHeader"]),
			self._settings.get(["server", "reverseProxy", "hostHeader"]),
			self._settings.get(["server", "reverseProxy", "prefixFallback"]),
			self._settings.get(["server", "reverseProxy", "schemeFallback"]),
			self._settings.get(["server", "reverseProxy", "hostFallback"])
		)

		secret_key = self._settings.get(["server", "secretKey"])
		if not secret_key:
			import string
			from random import choice
			chars = string.ascii_lowercase + string.ascii_uppercase + string.digits
			secret_key = "".join(choice(chars) for _ in range(32))
			self._settings.set(["server", "secretKey"], secret_key)
			self._settings.save()
		app.secret_key = secret_key
		loginManager = LoginManager()
		loginManager.session_protection = "strong"
		loginManager.user_callback = load_user
		if not userManager.enabled:
			loginManager.anonymous_user = users.DummyUser
			principals.identity_loaders.appendleft(users.dummy_identity_loader)
		loginManager.init_app(app)

		if self._host is None:
			self._host = self._settings.get(["server", "host"])
		if self._port is None:
			self._port = self._settings.getInt(["server", "port"])

		app.debug = self._debug

		# register API blueprint
		self._setup_blueprints()

		## Tornado initialization starts here

		ioloop = IOLoop()
		ioloop.install()

		self._router = SockJSRouter(self._create_socket_connection, "/sockjs")

		upload_suffixes = dict(name=self._settings.get(["server", "uploads", "nameSuffix"]), path=self._settings.get(["server", "uploads", "pathSuffix"]))

		def mime_type_guesser(path):
			from octoprint.filemanager import get_mime_type
			return get_mime_type(path)

		download_handler_kwargs = dict(
			as_attachment=True,
			allow_client_caching=False
		)
		additional_mime_types=dict(mime_type_guesser=mime_type_guesser)
		admin_validator = dict(access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.user_validator))
		no_hidden_files_validator = dict(path_validation=util.tornado.path_validation_factory(lambda path: not octoprint.util.is_hidden_path(path), status_code=404))

		def joined_dict(*dicts):
			if not len(dicts):
				return dict()

			joined = dict()
			for d in dicts:
				joined.update(d)
			return joined

		server_routes = self._router.urls + [
			# various downloads
			# .mpg and .mp4 timelapses:
			(r"/downloads/timelapse/([^/]*\.mp[g4])", util.tornado.LargeResponseHandler, joined_dict(dict(path=self._settings.getBaseFolder("timelapse")),
			                                                                                      download_handler_kwargs,
			                                                                                      no_hidden_files_validator)),
			(r"/downloads/files/local/(.*)", util.tornado.LargeResponseHandler, joined_dict(dict(path=self._settings.getBaseFolder("uploads")),
			                                                                                download_handler_kwargs,
			                                                                                no_hidden_files_validator,
			                                                                                additional_mime_types)),
			(r"/downloads/logs/([^/]*)", util.tornado.LargeResponseHandler, joined_dict(dict(path=self._settings.getBaseFolder("logs")),
			                                                                            download_handler_kwargs,
			                                                                            admin_validator)),
			# camera snapshot
			(r"/downloads/camera/current", util.tornado.UrlProxyHandler, dict(url=self._settings.get(["webcam", "snapshot"]),
			                                                                  as_attachment=True,
			                                                                  access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.user_validator))),
			# generated webassets
			(r"/static/webassets/(.*)", util.tornado.LargeResponseHandler, dict(path=os.path.join(self._settings.getBaseFolder("generated"), "webassets"))),

			# online indicators - text file with "online" as content and a transparent gif
			(r"/online.txt", util.tornado.StaticDataHandler, dict(data="online\n")),
			(r"/online.gif", util.tornado.StaticDataHandler, dict(data=bytes(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")),
			                                                      content_type="image/gif"))
		]
		for name, hook in pluginManager.get_hooks("octoprint.server.http.routes").items():
			try:
				result = hook(list(server_routes))
			except:
				self._logger.exception("There was an error while retrieving additional server routes from plugin hook {name}".format(**locals()))
			else:
				if isinstance(result, (list, tuple)):
					for entry in result:
						if not isinstance(entry, tuple) or not len(entry) == 3:
							continue
						if not isinstance(entry[0], basestring):
							continue
						if not isinstance(entry[2], dict):
							continue

						route, handler, kwargs = entry
						route = r"/plugin/{name}/{route}".format(name=name, route=route if not route.startswith("/") else route[1:])

						self._logger.debug("Adding additional route {route} handled by handler {handler} and with additional arguments {kwargs!r}".format(**locals()))
						server_routes.append((route, handler, kwargs))

		server_routes.append((r".*", util.tornado.UploadStorageFallbackHandler, dict(fallback=util.tornado.WsgiInputContainer(app.wsgi_app), file_prefix="octoprint-file-upload-", file_suffix=".tmp", suffixes=upload_suffixes)))

		self._tornado_app = Application(server_routes)
		max_body_sizes = [
			("POST", r"/api/files/([^/]*)", self._settings.getInt(["server", "uploads", "maxSize"])),
			("POST", r"/api/languages", 5 * 1024 * 1024)
		]

		# allow plugins to extend allowed maximum body sizes
		for name, hook in pluginManager.get_hooks("octoprint.server.http.bodysize").items():
			try:
				result = hook(list(max_body_sizes))
			except:
				self._logger.exception("There was an error while retrieving additional upload sizes from plugin hook {name}".format(**locals()))
			else:
				if isinstance(result, (list, tuple)):
					for entry in result:
						if not isinstance(entry, tuple) or not len(entry) == 3:
							continue
						if not entry[0] in util.tornado.UploadStorageFallbackHandler.BODY_METHODS:
							continue
						if not isinstance(entry[2], int):
							continue

						method, route, size = entry
						route = r"/plugin/{name}/{route}".format(name=name, route=route if not route.startswith("/") else route[1:])

						self._logger.debug("Adding maximum body size of {size}B for {method} requests to {route})".format(**locals()))
						max_body_sizes.append((method, route, size))

		self._stop_intermediary_server()

		self._server = util.tornado.CustomHTTPServer(self._tornado_app, max_body_sizes=max_body_sizes, default_max_body_size=self._settings.getInt(["server", "maxSize"]))
		self._server.listen(self._port, address=self._host)

		eventManager.fire(events.Events.STARTUP)
		if self._settings.getBoolean(["serial", "autoconnect"]):
			(port, baudrate) = self._settings.get(["serial", "port"]), self._settings.getInt(["serial", "baudrate"])
			printer_profile = printerProfileManager.get_default()
			connectionOptions = printer.__class__.get_connection_options()
			if port in connectionOptions["ports"]:
				printer.connect(port=port, baudrate=baudrate, profile=printer_profile["id"] if "id" in printer_profile else "_default")

		# start up watchdogs
		if self._settings.getBoolean(["feature", "pollWatched"]):
			# use less performant polling observer if explicitely configured
			observer = PollingObserver()
		else:
			# use os default
			observer = Observer()
		observer.schedule(util.watchdog.GcodeWatchdogHandler(fileManager, printer), self._settings.getBaseFolder("watched"))
		observer.start()

		# run our startup plugins
		octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
		                             "on_startup",
		                             args=(self._host, self._port),
		                             sorting_context="StartupPlugin.on_startup")

		def call_on_startup(name, plugin):
			implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
			if implementation is None:
				return
			implementation.on_startup(self._host, self._port)
		pluginLifecycleManager.add_callback("enabled", call_on_startup)

		# prepare our after startup function
		def on_after_startup():
			self._logger.info("Listening on http://%s:%d" % (self._host, self._port))

			# now this is somewhat ugly, but the issue is the following: startup plugins might want to do things for
			# which they need the server to be already alive (e.g. for being able to resolve urls, such as favicons
			# or service xmls or the like). While they are working though the ioloop would block. Therefore we'll
			# create a single use thread in which to perform our after-startup-tasks, start that and hand back
			# control to the ioloop
			def work():
				octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
				                             "on_after_startup",
				                             sorting_context="StartupPlugin.on_after_startup")

				def call_on_after_startup(name, plugin):
					implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
					if implementation is None:
						return
					implementation.on_after_startup()
				pluginLifecycleManager.add_callback("enabled", call_on_after_startup)

				# when we are through with that we also run our preemptive cache
				if settings().getBoolean(["devel", "cache", "preemptive"]):
					self._execute_preemptive_flask_caching(preemptiveCache)

			import threading
			threading.Thread(target=work).start()
		ioloop.add_callback(on_after_startup)

		# prepare our shutdown function
		def on_shutdown():
			# will be called on clean system exit and shutdown the watchdog observer and call the on_shutdown methods
			# on all registered ShutdownPlugins
			self._logger.info("Shutting down...")
			observer.stop()
			observer.join()
			octoprint.plugin.call_plugin(octoprint.plugin.ShutdownPlugin,
			                             "on_shutdown",
			                             sorting_context="ShutdownPlugin.on_shutdown")

			if self._octoprint_daemon is not None:
				self._logger.info("Cleaning up daemon pidfile")
				self._octoprint_daemon.terminated()

			self._logger.info("Goodbye!")
		atexit.register(on_shutdown)

		def sigterm_handler(*args, **kwargs):
			# will stop tornado on SIGTERM, making the program exit cleanly
			def shutdown_tornado():
				ioloop.stop()
			ioloop.add_callback_from_signal(shutdown_tornado)
		signal.signal(signal.SIGTERM, sigterm_handler)

		try:
			# this is the main loop - as long as tornado is running, OctoPrint is running
			ioloop.start()
		except (KeyboardInterrupt, SystemExit):
			pass
		except:
			self._logger.fatal("Now that is embarrassing... Something really really went wrong here. Please report this including the stacktrace below in OctoPrint's bugtracker. Thanks!")
			self._logger.exception("Stacktrace follows:")

	def _create_socket_connection(self, session):
		global printer, fileManager, analysisQueue, userManager, eventManager
		return util.sockjs.PrinterStateConnection(printer, fileManager, analysisQueue, userManager, eventManager, pluginManager, session)

	def _check_for_root(self):
		if "geteuid" in dir(os) and os.geteuid() == 0:
			exit("You should not run OctoPrint as root!")

	def _get_locale(self):
		global LANGUAGES

		if "l10n" in request.values:
			return Locale.negotiate([request.values["l10n"]], LANGUAGES)

		if hasattr(g, "identity") and g.identity and userManager.enabled:
			userid = g.identity.id
			try:
				user_language = userManager.getUserSetting(userid, ("interface", "language"))
				if user_language is not None and not user_language == "_default":
					return Locale.negotiate([user_language], LANGUAGES)
			except octoprint.users.UnknownUser:
				pass

		default_language = self._settings.get(["appearance", "defaultLanguage"])
		if default_language is not None and not default_language == "_default" and default_language in LANGUAGES:
			return Locale.negotiate([default_language], LANGUAGES)

		return Locale.parse(request.accept_languages.best_match(LANGUAGES))

	def _setup_app(self):
		@app.before_request
		def before_request():
			g.locale = self._get_locale()

		@app.after_request
		def after_request(response):
			# send no-cache headers with all POST responses
			if request.method == "POST":
				response.cache_control.no_cache = True
			response.headers.add("X-Clacks-Overhead", "GNU Terry Pratchett")
			return response

		Markdown(app)

	def _setup_i18n(self, app):
		global babel
		global LOCALES
		global LANGUAGES

		babel = Babel(app)

		def get_available_locale_identifiers(locales):
			result = set()

			# add available translations
			for locale in locales:
				result.add(locale.language)
				if locale.territory:
					# if a territory is specified, add that too
					result.add("%s_%s" % (locale.language, locale.territory))

			return result

		LOCALES = babel.list_translations()
		LANGUAGES = get_available_locale_identifiers(LOCALES)

		@babel.localeselector
		def get_locale():
			return self._get_locale()

	def _setup_jinja2(self):
		import re

		app.jinja_env.add_extension("jinja2.ext.do")
		app.jinja_env.add_extension("octoprint.util.jinja.trycatch")

		def regex_replace(s, find, replace):
			return re.sub(find, replace, s)

		html_header_regex = re.compile("<h(?P<number>[1-6])>(?P<content>.*?)</h(?P=number)>")
		def offset_html_headers(s, offset):
			def repl(match):
				number = int(match.group("number"))
				number += offset
				if number > 6:
					number = 6
				elif number < 1:
					number = 1
				return "<h{number}>{content}</h{number}>".format(number=number, content=match.group("content"))
			return html_header_regex.sub(repl, s)

		markdown_header_regex = re.compile("^(?P<hashs>#+)\s+(?P<content>.*)$", flags=re.MULTILINE)
		def offset_markdown_headers(s, offset):
			def repl(match):
				number = len(match.group("hashs"))
				number += offset
				if number > 6:
					number = 6
				elif number < 1:
					number = 1
				return "{hashs} {content}".format(hashs="#" * number, content=match.group("content"))
			return markdown_header_regex.sub(repl, s)

		html_link_regex = re.compile("<(?P<tag>a.*?)>(?P<content>.*?)</a>")
		def externalize_links(text):
			def repl(match):
				tag = match.group("tag")
				if not u"href" in tag:
					return match.group(0)

				if not u"target=" in tag and not u"rel=" in tag:
					tag += u" target=\"_blank\" rel=\"noreferrer noopener\""

				content = match.group("content")
				return u"<{tag}>{content}</a>".format(tag=tag, content=content)
			return html_link_regex.sub(repl, text)

		app.jinja_env.filters["regex_replace"] = regex_replace
		app.jinja_env.filters["offset_html_headers"] = offset_html_headers
		app.jinja_env.filters["offset_markdown_headers"] = offset_markdown_headers
		app.jinja_env.filters["externalize_links"] = externalize_links

		# configure additional template folders for jinja2
		import jinja2
		import octoprint.util.jinja
		filesystem_loader = octoprint.util.jinja.FilteredFileSystemLoader([],
		                                                                  path_filter=lambda x: not octoprint.util.is_hidden_path(x))
		filesystem_loader.searchpath = self._template_searchpaths

		loaders = [app.jinja_loader, filesystem_loader]
		if octoprint.util.is_running_from_source():
			root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
			allowed = ["AUTHORS.md", "CHANGELOG.md", "SUPPORTERS.md", "THIRDPARTYLICENSES.md"]
			files = {"_data/" + name: os.path.join(root, name) for name in allowed}
			loaders.append(octoprint.util.jinja.SelectedFilesLoader(files))

		jinja_loader = jinja2.ChoiceLoader(loaders)
		app.jinja_loader = jinja_loader

		self._register_template_plugins()

	def _execute_preemptive_flask_caching(self, preemptive_cache):
		from werkzeug.test import EnvironBuilder
		import time

		# we clean up entries from our preemptive cache settings that haven't been
		# accessed longer than server.preemptiveCache.until days
		preemptive_cache_timeout = settings().getInt(["server", "preemptiveCache", "until"])
		cutoff_timestamp = time.time() - preemptive_cache_timeout * 24 * 60 * 60

		def filter_current_entries(entry):
			"""Returns True for entries younger than the cutoff date"""
			return "_timestamp" in entry and entry["_timestamp"] > cutoff_timestamp

		def filter_http_entries(entry):
			"""Returns True for entries targeting http or https."""
			return "base_url" in entry \
			       and entry["base_url"] \
			       and (entry["base_url"].startswith("http://")
			            or entry["base_url"].startswith("https://"))

		def filter_entries(entry):
			"""Combined filter."""
			filters = (filter_current_entries,
			           filter_http_entries)
			return all([f(entry) for f in filters])

		# filter out all old and non-http entries
		cache_data = preemptive_cache.clean_all_data(lambda root, entries: filter(filter_entries, entries))
		if not cache_data:
			return

		def execute_caching():
			for route in sorted(cache_data.keys(), key=lambda x: (x.count("/"), x)):
				entries = reversed(sorted(cache_data[route], key=lambda x: x.get("_count", 0)))
				for kwargs in entries:
					plugin = kwargs.get("plugin", None)
					if plugin:
						try:
							plugin_info = pluginManager.get_plugin_info(plugin, require_enabled=True)
							if plugin_info is None:
								self._logger.debug("Plugin {} is not installed or enabled, preemptive caching makes no sense".format(plugin))
								continue

							implementation = plugin_info.implementation
							if implementation is None or not isinstance(implementation, octoprint.plugin.UiPlugin):
								self._logger.debug("Plugin {} is not a UiPlugin, preemptive caching makes no sense".format(plugin))
								continue
							if not implementation.get_ui_preemptive_caching_enabled():
								self._logger.debug("Plugin {} has disabled preemptive caching".format(plugin))
								continue
						except:
							self._logger.exception("Error while trying to check if plugin {} has preemptive caching enabled, skipping entry")
							continue

					additional_request_data = kwargs.get("_additional_request_data", dict())
					kwargs = dict((k, v) for k, v in kwargs.items() if not k.startswith("_") and not k == "plugin")
					kwargs.update(additional_request_data)

					try:
						if plugin:
							self._logger.info("Preemptively caching {} (plugin {}) for {!r}".format(route, plugin, kwargs))
						else:
							self._logger.info("Preemptively caching {} for {!r}".format(route, kwargs))
						builder = EnvironBuilder(**kwargs)
						with preemptive_cache.cache_environment(dict(plugin=plugin if plugin is not None else "_default")):
							with preemptive_cache.disable_access_logging():
								app(builder.get_environ(), lambda *a, **kw: None)
					except:
						self._logger.exception("Error while trying to preemptively cache {} for {!r}".format(route, kwargs))

		# asynchronous caching
		import threading
		cache_thread = threading.Thread(target=execute_caching, name="Preemptive Cache Worker")
		cache_thread.daemon = True
		cache_thread.start()

	def _register_template_plugins(self):
		template_plugins = pluginManager.get_implementations(octoprint.plugin.TemplatePlugin)
		for plugin in template_plugins:
			try:
				self._register_additional_template_plugin(plugin)
			except:
				self._logger.exception("Error while trying to register templates of plugin {}, ignoring it".format(plugin._identifier))

	def _register_additional_template_plugin(self, plugin):
		folder = plugin.get_template_folder()
		if folder is not None and not folder in self._template_searchpaths:
			self._template_searchpaths.append(folder)

	def _unregister_additional_template_plugin(self, plugin):
		folder = plugin.get_template_folder()
		if folder is not None and folder in self._template_searchpaths:
			self._template_searchpaths.remove(folder)

	def _setup_blueprints(self):
		from octoprint.server.api import api
		from octoprint.server.apps import apps, clear_registered_app
		import octoprint.server.views

		app.register_blueprint(api, url_prefix="/api")
		app.register_blueprint(apps, url_prefix="/apps")

		# also register any blueprints defined in BlueprintPlugins
		self._register_blueprint_plugins()

		# and register a blueprint for serving the static files of asset plugins which are not blueprint plugins themselves
		self._register_asset_plugins()

		global pluginLifecycleManager
		def clear_apps(name, plugin):
			clear_registered_app()
		pluginLifecycleManager.add_callback("enabled", clear_apps)
		pluginLifecycleManager.add_callback("disabled", clear_apps)

	def _register_blueprint_plugins(self):
		blueprint_plugins = octoprint.plugin.plugin_manager().get_implementations(octoprint.plugin.BlueprintPlugin)
		for plugin in blueprint_plugins:
			try:
				self._register_blueprint_plugin(plugin)
			except:
				self._logger.exception("Error while registering blueprint of plugin {}, ignoring it".format(plugin._identifier))
				continue

	def _register_asset_plugins(self):
		asset_plugins = octoprint.plugin.plugin_manager().get_implementations(octoprint.plugin.AssetPlugin)
		for plugin in asset_plugins:
			if isinstance(plugin, octoprint.plugin.BlueprintPlugin):
				continue
			try:
				self._register_asset_plugin(plugin)
			except:
				self._logger.exception("Error while registering assets of plugin {}, ignoring it".format(plugin._identifier))
				continue

	def _register_blueprint_plugin(self, plugin):
		name = plugin._identifier
		blueprint = plugin.get_blueprint()
		if blueprint is None:
			return

		if plugin.is_blueprint_protected():
			from octoprint.server.util import apiKeyRequestHandler, corsResponseHandler
			blueprint.before_request(apiKeyRequestHandler)
			blueprint.after_request(corsResponseHandler)

		url_prefix = "/plugin/{name}".format(name=name)
		app.register_blueprint(blueprint, url_prefix=url_prefix)

		if self._logger:
			self._logger.debug("Registered API of plugin {name} under URL prefix {url_prefix}".format(name=name, url_prefix=url_prefix))

	def _register_asset_plugin(self, plugin):
		name = plugin._identifier

		url_prefix = "/plugin/{name}".format(name=name)
		blueprint = Blueprint("plugin." + name, name, static_folder=plugin.get_asset_folder())
		app.register_blueprint(blueprint, url_prefix=url_prefix)

		if self._logger:
			self._logger.debug("Registered assets of plugin {name} under URL prefix {url_prefix}".format(name=name, url_prefix=url_prefix))

	def _setup_assets(self):
		global app
		global assets
		global pluginManager

		util.flask.fix_webassets_cache()
		util.flask.fix_webassets_filtertool()

		base_folder = self._settings.getBaseFolder("generated")

		# clean the folder
		if self._settings.getBoolean(["devel", "webassets", "clean_on_startup"]):
			import shutil
			import errno
			import sys

			for entry in ("webassets", ".webassets-cache"):
				path = os.path.join(base_folder, entry)

				# delete path if it exists
				if os.path.isdir(path):
					try:
						self._logger.debug("Deleting {path}...".format(**locals()))
						shutil.rmtree(path)
					except:
						self._logger.exception("Error while trying to delete {path}, leaving it alone".format(**locals()))
						continue

				# re-create path
				self._logger.debug("Creating {path}...".format(**locals()))
				error_text = "Error while trying to re-create {path}, that might cause errors with the webassets cache".format(**locals())
				try:
					os.makedirs(path)
				except OSError as e:
					if e.errno == errno.EACCES:
						# that might be caused by the user still having the folder open somewhere, let's try again after
						# waiting a bit
						import time
						for n in range(3):
							time.sleep(0.5)
							self._logger.debug("Creating {path}: Retry #{retry} after {time}s".format(path=path, retry=n+1, time=(n + 1)*0.5))
							try:
								os.makedirs(path)
								break
							except:
								if self._logger.isEnabledFor(logging.DEBUG):
									self._logger.exception("Ignored error while creating directory {path}".format(**locals()))
								pass
						else:
							# this will only get executed if we never did
							# successfully execute makedirs above
							self._logger.exception(error_text)
							continue
					else:
						# not an access error, so something we don't understand
						# went wrong -> log an error and stop
						self._logger.exception(error_text)
						continue
				except:
					# not an OSError, so something we don't understand
					# went wrong -> log an error and stop
					self._logger.exception(error_text)
					continue

				self._logger.info("Reset webasset folder {path}...".format(**locals()))

		AdjustedEnvironment = type(Environment)(Environment.__name__, (Environment,), dict(
			resolver_class=util.flask.PluginAssetResolver
		))
		class CustomDirectoryEnvironment(AdjustedEnvironment):
			@property
			def directory(self):
				return base_folder

		assets = CustomDirectoryEnvironment(app)
		assets.debug = not self._settings.getBoolean(["devel", "webassets", "bundle"])

		UpdaterType = type(util.flask.SettingsCheckUpdater)(util.flask.SettingsCheckUpdater.__name__, (util.flask.SettingsCheckUpdater,), dict(
			updater=assets.updater
		))
		assets.updater = UpdaterType

		enable_gcodeviewer = self._settings.getBoolean(["gcodeViewer", "enabled"])
		preferred_stylesheet = self._settings.get(["devel", "stylesheet"])
		minify = self._settings.getBoolean(["devel", "webassets", "minify"])

		dynamic_assets = util.flask.collect_plugin_assets(
			enable_gcodeviewer=enable_gcodeviewer,
			preferred_stylesheet=preferred_stylesheet
		)

		js_libs = [
			"js/lib/jquery/jquery-2.1.4.min.js" if minify else "js/lib/jquery/jquery-2.1.4.js",
			"js/lib/modernizr.custom.js",
			"js/lib/lodash.min.js",
			"js/lib/sprintf.min.js",
			"js/lib/knockout-3.4.0.js",
			"js/lib/knockout.mapping-latest.js",
			"js/lib/babel.js",
			"js/lib/avltree.js",
			"js/lib/bootstrap/bootstrap.js",
			"js/lib/bootstrap/bootstrap-modalmanager.js",
			"js/lib/bootstrap/bootstrap-modal.js",
			"js/lib/bootstrap/bootstrap-slider.js",
			"js/lib/bootstrap/bootstrap-tabdrop.js",
			"js/lib/jquery/jquery.ui.core.js",
			"js/lib/jquery/jquery.ui.widget.js",
			"js/lib/jquery/jquery.ui.mouse.js",
			"js/lib/jquery/jquery.flot.js",
			"js/lib/jquery/jquery.iframe-transport.js",
			"js/lib/jquery/jquery.fileupload.js",
			"js/lib/jquery/jquery.slimscroll.min.js",
			"js/lib/jquery/jquery.qrcode.min.js",
			"js/lib/jquery/jquery.bootstrap.wizard.js",
			"js/lib/moment-with-locales.min.js",
			"js/lib/pusher.color.min.js",
			"js/lib/detectmobilebrowser.js",
			"js/lib/md5.min.js",
			"js/lib/pnotify.min.js",
			"js/lib/bootstrap-slider-knockout-binding.js",
			"js/lib/loglevel.min.js",
			"js/lib/sockjs-0.3.4.min.js"
		]
		js_client = [
			"js/app/client/base.js",
			"js/app/client/socket.js",
			"js/app/client/browser.js",
			"js/app/client/connection.js",
			"js/app/client/control.js",
			"js/app/client/files.js",
			"js/app/client/job.js",
			"js/app/client/languages.js",
			"js/app/client/logs.js",
			"js/app/client/printer.js",
			"js/app/client/printerprofiles.js",
			"js/app/client/settings.js",
			"js/app/client/slicing.js",
			"js/app/client/system.js",
			"js/app/client/timelapse.js",
			"js/app/client/users.js",
			"js/app/client/util.js",
			"js/app/client/wizard.js"
		]
		js_app = dynamic_assets["js"] + [
			"js/app/dataupdater.js",
			"js/app/helpers.js",
			"js/app/main.js",
		]

		css_libs = [
			"css/bootstrap.min.css",
			"css/bootstrap-modal.css",
			"css/bootstrap-slider.css",
			"css/bootstrap-tabdrop.css",
			"css/font-awesome.min.css",
			"css/jquery.fileupload-ui.css",
			"css/pnotify.min.css"
		]
		css_app = list(dynamic_assets["css"])
		less_app = list(dynamic_assets["less"])

		from webassets.filter import register_filter, Filter
		from webassets.filter.cssrewrite.base import PatternRewriter
		import re
		class LessImportRewrite(PatternRewriter):
			name = "less_importrewrite"

			patterns = {
				"import_rewrite": re.compile("(@import(\s+\(.*\))?\s+)\"(.*)\";")
			}

			def import_rewrite(self, m):
				import_with_options = m.group(1)
				import_url = m.group(3)

				if not import_url.startswith("http:") and not import_url.startswith("https:") and not import_url.startswith("/"):
					import_url = "../less/" + import_url

				return "{import_with_options}\"{import_url}\";".format(**locals())

		class JsDelimiterBundle(Filter):
			name = "js_delimiter_bundler"
			options = {}
			def input(self, _in, out, **kwargs):
				out.write(_in.read())
				out.write("\n;\n")

		register_filter(LessImportRewrite)
		register_filter(JsDelimiterBundle)

		js_libs_bundle = Bundle(*js_libs, output="webassets/packed_libs.js", filters="js_delimiter_bundler")
		if minify:
			js_client_bundle = Bundle(*js_client, output="webassets/packed_client.js", filters="rjsmin, js_delimiter_bundler")
			js_app_bundle = Bundle(*js_app, output="webassets/packed_app.js", filters="rjsmin, js_delimiter_bundler")
		else:
			js_client_bundle = Bundle(*js_client, output="webassets/packed_client.js", filters="js_delimiter_bundler")
			js_app_bundle = Bundle(*js_app, output="webassets/packed_app.js", filters="js_delimiter_bundler")

		css_libs_bundle = Bundle(*css_libs, output="webassets/packed_libs.css")

		if len(css_app) == 0:
			css_app_bundle = Bundle(*[])
		else:
			css_app_bundle = Bundle(*css_app, output="webassets/packed_app.css", filters="cssrewrite")

		if len(less_app) == 0:
			all_less_bundle = Bundle(*[])
		else:
			all_less_bundle = Bundle(*less_app, output="webassets/packed_app.less", filters="cssrewrite, less_importrewrite")

		assets.register("js_libs", js_libs_bundle)
		assets.register("js_client", js_client_bundle)
		assets.register("js_app", js_app_bundle)
		assets.register("css_libs", css_libs_bundle)
		assets.register("css_app", css_app_bundle)
		assets.register("less_app", all_less_bundle)

	def _start_intermediary_server(self):
		import BaseHTTPServer
		import SimpleHTTPServer
		import threading

		host = self._host
		port = self._port
		if host is None:
			host = self._settings.get(["server", "host"])
		if port is None:
			port = self._settings.getInt(["server", "port"])

		self._logger.debug("Starting intermediary server on {}:{}".format(host, port))

		class IntermediaryServerHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
			def __init__(self, rules=None, *args, **kwargs):
				if rules is None:
					rules = []
				self.rules = rules
				SimpleHTTPServer.SimpleHTTPRequestHandler.__init__(self, *args, **kwargs)

			def do_GET(self):
				request_path = self.path
				if "?" in request_path:
					request_path = request_path[0:request_path.find("?")]

				for rule in self.rules:
					path, data, content_type = rule
					if request_path == path:
						self.send_response(200)
						if content_type:
							self.send_header("Content-Type", content_type)
						self.end_headers()
						self.wfile.write(data)
						break
				else:
					self.send_response(404)
					self.wfile.write("Not found")

		base_path = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "static"))
		rules = [
			("/", ["intermediary.html",], "text/html"),
			("/favicon.ico", ["img", "tentacle-20x20.png"], "image/png"),
			("/intermediary.gif", bytes(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")), "image/gif")
		]

		def contents(args):
			path = os.path.join(base_path, *args)
			if not os.path.isfile(path):
				return ""

			with open(path, "rb") as f:
				data = f.read()
			return data

		def process(rule):
			if len(rule) == 2:
				path, data = rule
				content_type = None
			else:
				path, data, content_type = rule

			if isinstance(data, (list, tuple)):
				data = contents(data)

			return path, data, content_type

		rules = map(process, filter(lambda rule: len(rule) == 2 or len(rule) == 3, rules))

		self._intermediary_server = BaseHTTPServer.HTTPServer((host, port), lambda *args, **kwargs: IntermediaryServerHandler(rules, *args, **kwargs))

		thread = threading.Thread(target=self._intermediary_server.serve_forever)
		thread.daemon = True
		thread.start()

		self._logger.debug("Intermediary server started")

	def _stop_intermediary_server(self):
		if self._intermediary_server is None:
			return
		self._logger.debug("Shutting down intermediary server...")
		self._intermediary_server.shutdown()
		self._intermediary_server.server_close()
		self._logger.debug("Intermediary server shut down")

class LifecycleManager(object):
	def __init__(self, plugin_manager):
		self._plugin_manager = plugin_manager

		self._plugin_lifecycle_callbacks = defaultdict(list)
		self._logger = logging.getLogger(__name__)

		def wrap_plugin_event(lifecycle_event, new_handler):
			orig_handler = getattr(self._plugin_manager, "on_plugin_" + lifecycle_event)

			def handler(*args, **kwargs):
				if callable(orig_handler):
					orig_handler(*args, **kwargs)
				if callable(new_handler):
					new_handler(*args, **kwargs)

			return handler

		def on_plugin_event_factory(lifecycle_event):
			def on_plugin_event(name, plugin):
				self.on_plugin_event(lifecycle_event, name, plugin)
			return on_plugin_event

		for event in ("loaded", "unloaded", "enabled", "disabled"):
			wrap_plugin_event(event, on_plugin_event_factory(event))

	def on_plugin_event(self, event, name, plugin):
		for lifecycle_callback in self._plugin_lifecycle_callbacks[event]:
			lifecycle_callback(name, plugin)

	def add_callback(self, events, callback):
		if isinstance(events, (str, unicode)):
			events = [events]

		for event in events:
			self._plugin_lifecycle_callbacks[event].append(callback)

	def remove_callback(self, callback, events=None):
		if events is None:
			for event in self._plugin_lifecycle_callbacks:
				if callback in self._plugin_lifecycle_callbacks[event]:
					self._plugin_lifecycle_callbacks[event].remove(callback)
		else:
			if isinstance(events, (str, unicode)):
				events = [events]

			for event in events:
				if callback in self._plugin_lifecycle_callbacks[event]:
					self._plugin_lifecycle_callbacks[event].remove(callback)

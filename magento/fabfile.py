from fabric.api import *
import os
import sys
import random
import string
import ConfigParser
# Custom Code Enigma modules
import common.ConfigFile
import common.Services
import common.Utils
import common.Tests
import common.PHP
import common.MySQL
import Magento
import InitialBuild
import AdjustConfiguration

# Override the shell env variable in Fabric, so that we don't see 
# pesky 'stdin is not a tty' messages when using sudo
env.shell = '/bin/bash -c'


######
def main(repo, repourl, branch, build, buildtype, shared_static_dir=False, db_name=None, db_username=None, db_password=None, dump_file=None, keepbuilds=10, buildtype_override=False, httpauth_pass=None, cluster=False, with_no_dev=True, statuscakeuser=None, statuscakekey=None, statuscakeid=None, webserverport='8080', mysql_version=5.5, rds=False, autoscale=None, mysql_config='/etc/mysql/debian.cnf', config_filename='config.ini', www_root='/var/www'):

  # shared_static_dir:
  #   on dev this is dynamically generated and stored in /var/www/shared
  #   on prod it's generated on build and kept in the build folder
  
  # Read the config.ini file from repo, if it exists
  config = common.ConfigFile.buildtype_config_file(buildtype, config_filename)

  # Now we need to figure out what server(s) we're working with
  # Define primary host
  common.Utils.define_host(config, buildtype, repo)
  # Define server roles (if applicable)
  common.Utils.define_roles(config, cluster, autoscale)
  # Check where we're deploying to - abort if nothing set in config.ini
  if env.host is None:
    raise ValueError("===> You wanted to deploy a build but we couldn't find a host in the map file for repo %s so we're aborting." % repo)

  # Set some default config options and variables
  user = "jenkins"
  previous_build = ""
  previous_db = ""
  statuscake_paused = False
  site_root = www_root + '/%s_%s_%s' % (repo, buildtype, build)
  site_link = www_root + '/live.%s.%s' % (repo, buildtype)

  # Determine web server
  webserver = "nginx"
  with settings(hide('running', 'warnings', 'stdout', 'stderr'), warn_only=True):
    services = ['apache2', 'httpd']
    for service in services:
      if run('pgrep -lf %s | egrep -v "bash|grep" > /dev/null' % service).return_code == 0:
        webserver = service

  # Set our host_string based on user@host
  env.host_string = '%s@%s' % (user, env.host)

  # Can be set in the config.ini [Build] section
  ssh_key = common.ConfigFile.return_config_item(config, "Build", "ssh_key")
  notifications_email = common.ConfigFile.return_config_item(config, "Build", "notifications_email")
  # Need to keep potentially passed in 'url' value as default
  url = common.ConfigFile.return_config_item(config, "Build", "url", "string", url)

  # Can be set in the config.ini [Database] section
  db_name = common.ConfigFile.return_config_item(config, "Database", "db_name")
  db_username = common.ConfigFile.return_config_item(config, "Database", "db_username")
  db_password = common.ConfigFile.return_config_item(config, "Database", "db_password")
  # Need to keep potentially passed in MySQL version and config path as defaults
  mysql_config = common.ConfigFile.return_config_item(config, "Database", "mysql_config", "string", mysql_config)
  mysql_version = common.ConfigFile.return_config_item(config, "Database", "mysql_version", "string", mysql_version)
  dump_file = common.ConfigFile.return_config_item(config, "Database", "dump_file")

  # Can be set in the config.ini [Composer] section
  composer = common.ConfigFile.return_config_item(config, "Composer", "composer", "boolean", True)
  composer_lock = common.ConfigFile.return_config_item(config, "Composer", "composer_lock", "boolean", True)
  no_dev = common.ConfigFile.return_config_item(config, "Composer", "no_dev", "boolean", True)

  # Can be set in the config.ini [Testing] section
  # PHPUnit is in common/Tests because it can be used for any PHP application
  phpunit_run = common.ConfigFile.return_config_item(config, "Testing", "phpunit_run", "boolean", False)
  phpunit_fail_build = common.ConfigFile.return_config_item(config, "Testing", "phpunit_fail_build", "boolean", False)
  phpunit_group = common.ConfigFile.return_config_item(config, "Testing", "phpunit_group", "string", "unit")
  phpunit_test_directory = common.ConfigFile.return_config_item(config, "Testing", "phpunit_test_directory")
  phpunit_path = common.ConfigFile.return_config_item(config, "Testing", "phpunit_path", "string", "vendor/phpunit/phpunit/phpunit")
  # CodeSniffer itself is in common/Tests, but standards used here are Drupal specific, see drupal/DrupalTests.py for the wrapper to apply them
  codesniffer = common.ConfigFile.return_config_item(config, "Testing", "codesniffer", "boolean")
  codesniffer_extensions = common.ConfigFile.return_config_item(config, "Testing", "codesniffer_extensions", "string", "php,module,inc,install,test,profile,theme,info,txt,md")
  codesniffer_ignore = common.ConfigFile.return_config_item(config, "Testing", "codesniffer_ignore", "string", "node_modules,bower_components,vendor")
  codesniffer_paths = common.ConfigFile.return_config_item(config, "Testing", "codesniffer_paths")

  # Run the tasks.
  execute(common.Utils.clone_repo, repo, repourl, branch, build, None, ssh_key, hosts=env.roledefs['app_all'])

  # If this is the first build, attempt to install the site for the first time.
  with settings(hide('warnings', 'stderr'), warn_only=True):
    if run("find %s -type f -name mage" % (site_link)).failed:
      fresh_install = True
    else:
      fresh_install = False

  if fresh_install is True:
    print "===> Looks like the site %s doesn't exist. We'll try and install it..." % url

    # Check for expected shared directories
    execute(common.Utils.create_config_directory, hosts=env.roledefs['app_all'])
    execute(common.Utils.create_shared_directory, hosts=env.roledefs['app_all'])
    execute(common.Utils.initial_build_create_live_symlink, repo, buildtype, build, hosts=env.roledefs['app_all'])

    try:
      execute(InitialBuild.initial_magento_build, repo, url, buildtype, build, shared_static_dir, config, rds, db_name, db_username, mysql_version, db_password, mysql_config, dump_file)
      execute(InitialBuild.initial_build_vhost, webserver, repo, buildtype, url)
      if httpauth_pass:
        common.Utils.create_httpauth(webserver, repo, buildtype, url, httpauth_pass)
      # Restart services
      execute(common.Services.clear_php_cache, hosts=env.roledefs['app_all'])
      execute(common.Services.clear_varnish_cache, hosts=env.roledefs['app_all'])
      execute(common.Services.reload_webserver, hosts=env.roledefs['app_all'])

      # @TODO: what about cron??
    except:
      e = sys.exc_info()[1]
      raise SystemError(e)


  # Not an initial build, let's rebuild the site
  else:
    print "===> Looks like the site %s exists already. We'll try and launch a new build..." % url
    try:
      print "===> Taking a database backup of the Magento database..."
      # Get the credentials for Magento in order to be able to dump the database
      with settings(hide('stdout', 'running')):
        mage_db = run("grep dbname /var/www/live.%s.%s/www/app/etc/env.php | awk {'print $3'} | head -1 | cut -d\\' -f2" % (repo, buildtype))
      execute(common.MySQL.mysql_backup_db, db_name, build, True)      
      execute(Magento.adjust_files_symlink, repo, buildtype, build, url, shared_static_dir)
      execute(common.adjust_live_symlink, repo, branch, build, buildtype)
      # Restart services
      execute(common.Services.clear_php_cache, hosts=env.roledefs['app_all'])
      execute(common.Services.clear_varnish_cache, hosts=env.roledefs['app_all'])
      execute(common.Services.reload_webserver, hosts=env.roledefs['app_all'])
      # @TODO: Why is this not an initial build task?
      # @TODO: function needs work too in Magento.py!
      execute(Magento.generate_magento_cron, repo, environment)
      execute(common.Utils.remove_old_builds, repo, branch, keepbuilds, hosts=env.roledefs['app_all'])
    except:
      e = sys.exc_info()[1]
      raise SystemError(e)
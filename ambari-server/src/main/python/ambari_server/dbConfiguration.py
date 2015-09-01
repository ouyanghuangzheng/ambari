#!/usr/bin/env python

'''
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''
import glob
import os

from ambari_commons import OSConst
from ambari_commons.exceptions import FatalException
from ambari_commons.logging_utils import get_silent, print_error_msg, print_info_msg, print_warning_msg, set_silent
from ambari_commons.os_family_impl import OsFamilyImpl
from ambari_commons.str_utils import cbool
from ambari_server.serverConfiguration import decrypt_password_for_alias, get_ambari_properties, get_is_secure, \
  get_resources_location, get_value_from_properties, is_alias_string, \
  JDBC_PASSWORD_PROPERTY, JDBC_RCA_PASSWORD_ALIAS, PRESS_ENTER_MSG, DEFAULT_DBMS_PROPERTY
from ambari_server.userInput import get_validated_string_input


#Database settings
DB_STATUS_RUNNING_DEFAULT = "running"

SETUP_DB_CONNECT_TIMEOUT = 5
SETUP_DB_CONNECT_ATTEMPTS = 3

USERNAME_PATTERN = "^[a-zA-Z_][a-zA-Z0-9_\-]*$"
PASSWORD_PATTERN = "^[a-zA-Z0-9_-]*$"
DATABASE_NAMES = ["postgres", "oracle", "mysql", "mssql", "sqlanywhere"]
DATABASE_FULL_NAMES = {"oracle": "Oracle", "mysql": "MySQL", "mssql": "Microsoft SQL Server", "postgres":
  "PostgreSQL", "sqlanywhere": "SQL Anywhere"}

AMBARI_DATABASE_NAME = "ambari"
AMBARI_DATABASE_TITLE = "ambari"

STORAGE_TYPE_LOCAL = 'local'
STORAGE_TYPE_REMOTE = 'remote'

DEFAULT_USERNAME = "ambari"
DEFAULT_PASSWORD = "bigdata"

#
# Database configuration helper classes
#
class DBMSDesc:
  def __init__(self, i_dbms_key, i_storage_key, i_dbms_name, i_storage_name, i_fn_create_config):
    self.dbms_key = i_dbms_key
    self.storage_key = i_storage_key
    self.dbms_name = i_dbms_name
    self.storage_name = i_storage_name
    self.fn_create_config = i_fn_create_config

  def create_config(self, options, properties, dbId):
    return self.fn_create_config(options, properties, self.storage_key, dbId)

class DbPropKeys:
  def __init__(self, i_dbms_key, i_driver_key, i_server_key, i_port_key, i_db_name_key, i_db_url_key):
    self.dbms_key = i_dbms_key
    self.driver_key = i_driver_key
    self.server_key = i_server_key
    self.port_key = i_port_key
    self.db_name_key = i_db_name_key
    self.db_url_key = i_db_url_key

class DbAuthenticationKeys:
  def __init__(self, i_user_name_key, i_password_key, i_password_alias, i_password_filename):
    self.user_name_key = i_user_name_key
    self.password_key = i_password_key
    self.password_alias = i_password_alias
    self.password_filename = i_password_filename

#
# Database configuration base class
#
class DBMSConfig(object):
  def __init__(self, options, properties, storage_type):
    """
    #Just load the defaults. The derived classes will be able to modify them later
    """
    self.persistence_type = storage_type
    self.dbms = ""
    self.driver_class_name = ""
    self.driver_file_name = ""
    self.driver_symlink_name = ""
    self.database_host = ""
    self.database_port = ""
    self.database_name = ""
    self.database_username = ""

    self.db_title = AMBARI_DATABASE_TITLE

    self.must_set_database_options = DBMSConfig._init_member_with_default(options, "must_set_database_options", False)

    self.JDBC_DRIVER_INSTALL_MSG = 'Before starting Ambari Server, you must install the JDBC driver.'

    self.isSecure = get_is_secure(properties)
    pass


  #
  # Public methods
  #

  #
  # Main method. Configures the database according to the options and the existing properties.
  #
  def configure_database(self, properties):
    result = self._prompt_db_properties()
    if result:
      #DB setup should be done last after doing any setup.
      if self._is_local_database():
        self._setup_local_server(properties)
      else:
        self._setup_remote_server(properties)
    return result

  def setup_database(self):
    print 'Configuring {0} database...'.format(self.db_title)

    #DB setup should be done last after doing any setup.
    if self._is_local_database():
      self._setup_local_database()
    else:
      self._setup_remote_database()
    pass

  def reset_database(self):
    if self._is_local_database():
      self._reset_local_database()
    else:
      self._reset_remote_database()
    pass

  def ensure_jdbc_driver_installed(self, properties):
    (result, msg) = self._prompt_jdbc_driver_install(properties)
    if result == -1:
      print_error_msg(msg)
      raise FatalException(-1, msg)

    if result != 1:
      result = self._install_jdbc_driver(properties, result)
    return cbool(result)

  def change_db_files_owner(self):
    if self._is_local_database():
      retcode = self._change_db_files_owner()
      if not retcode == 0:
        raise FatalException(20, 'Unable to change owner of database objects')

  #
  # Private implementation
  #

  @staticmethod
  def _read_password_from_properties(properties):
    database_password = DEFAULT_PASSWORD
    password_file = get_value_from_properties(properties, JDBC_PASSWORD_PROPERTY, "")
    if password_file:
      if is_alias_string(password_file):
        database_password = decrypt_password_for_alias(properties, JDBC_RCA_PASSWORD_ALIAS)
      else:
        if os.path.isabs(password_file) and os.path.exists(password_file):
          with open(password_file, 'r') as file:
            database_password = file.read()
    return database_password

  @staticmethod
  def _init_member_with_default(options, attr_name, default_val):
    options_val = getattr(options, attr_name, None)
    val = options_val if options_val is not None and options_val is not "" else default_val
    return val

  @staticmethod
  def _init_member_with_properties(options, attr_name, properties, property_key):
    options_val = getattr(options, attr_name, None)
    if options_val is None or options_val is "":
      options_val = get_value_from_properties(properties, property_key, None)
    return options_val

  @staticmethod
  def _init_member_with_prop_default(options, attr_name, properties, property_key, default_val):
    val = DBMSConfig._init_member_with_properties(options, attr_name, properties, property_key)
    if val is None or val is "":
      val = default_val
    return val

  #
  # Checks if options determine local DB configuration
  #
  def _is_local_database(self):
    return self.persistence_type == STORAGE_TYPE_LOCAL

  def _prompt_db_properties(self):
    #if WINDOWS
    #  prompt for SQL Server host and instance name
    #else
    #  go the classic Linux way
    #linux_prompt_db_properties(args)
    return False

  def _setup_local_server(self, properties):
    pass

  def _setup_local_database(self):
    pass

  def _reset_local_database(self):
    pass

  def _setup_remote_server(self, properties):
    pass

  def _setup_remote_database(self):
    pass

  def _reset_remote_database(self):
    pass

  def _prompt_jdbc_driver_install(self, properties):
    result = self._is_jdbc_driver_installed(properties)
    if result == -1:
      if get_silent():
        print_error_msg(self.JDBC_DRIVER_INSTALL_MSG)
      else:
        print_warning_msg(self.JDBC_DRIVER_INSTALL_MSG)
        raw_input(PRESS_ENTER_MSG)
        result = self._is_jdbc_driver_installed(properties)
    return (result, self.JDBC_DRIVER_INSTALL_MSG)

  def _is_jdbc_driver_installed(self, properties):
    return 1

  def _install_jdbc_driver(self, properties, files_list):
    return False

  def ensure_dbms_is_running(self, options, properties, scmStatus=None):
    pass

  def _change_db_files_owner(self, args):
    return 0


#
# Database configuration factory base class
#
class DBMSConfigFactory(object):
  def select_dbms(self, options):
    '''
    # Base declaration of the DBMS selection method.
    :return: DBMS index in the descriptor table
    '''
    pass

  def create(self, options, properties, dbId = "Ambari"):
    """
    # Base declaration of the factory method. The outcome of the derived implementations
    #  is expected to be a subclass of DBMSConfig.
    # properties = property bag that will ultimately define the type of database. Since
    #   right now in Windows we only support SQL Server, this argument is not yet used.
    # dbId = additional information, that helps distinguish between various database connections, if applicable
    """
    pass

  def get_supported_dbms(self):
    return []

  def get_supported_jdbc_drivers(self):
    return []

  def force_dbms_setup(self):
    return False

  def get_default_dbms_name(self):
    return ""

#
# Database configuration factory for Windows
#
@OsFamilyImpl(os_family=OSConst.WINSRV_FAMILY)
class DBMSConfigFactoryWindows(DBMSConfigFactory):
  def __init__(self):
    from ambari_server.dbConfiguration_windows import DATABASE_DBMS_MSSQL

    self.DBMS_KEYS_LIST = [
      DATABASE_DBMS_MSSQL
    ]

  def select_dbms(self, options):
    # For now, we only support SQL Server in Windows, in remote mode.
    return 0

  def create(self, options, properties, dbId = "Ambari"):
    """
    # Windows implementation of the factory method. The outcome of the derived implementations
    #  is expected to be a subclass of DBMSConfig.
    # properties = property bag that will ultimately define the type of database. Since
    #   right now in Windows we only support SQL Server, this argument is not yet used.
    # dbId = additional information, that helps distinguish between various database connections, if applicable
    """
    from ambari_server.dbConfiguration_windows import createMSSQLConfig
    return createMSSQLConfig(options, properties, STORAGE_TYPE_REMOTE, dbId)

  def get_supported_dbms(self):
    return self.DBMS_KEYS_LIST

  def get_supported_jdbc_drivers(self):
    return self.DBMS_KEYS_LIST

#
# Database configuration factory for Linux
#
@OsFamilyImpl(os_family=OsFamilyImpl.DEFAULT)
class DBMSConfigFactoryLinux(DBMSConfigFactory):
  def __init__(self):
    from ambari_server.dbConfiguration_linux import createPGConfig, createOracleConfig, createMySQLConfig, \
      createMSSQLConfig, createSQLAConfig

    self.DBMS_KEYS_LIST = [
      'embedded',
      'oracle',
      'mysql',
      'postgres',
      'mssql',
      'sqlanywhere'
    ]

    self.DRIVER_KEYS_LIST = [
      'oracle',
      'mysql',
      'postgres',
      'mssql',
      'hsqldb',
      'sqlanywhere'
    ]

    self.DBMS_LIST = [
      DBMSDesc(self.DBMS_KEYS_LIST[3], STORAGE_TYPE_LOCAL, 'PostgreSQL', 'Embedded', createPGConfig),
      DBMSDesc(self.DBMS_KEYS_LIST[1], STORAGE_TYPE_REMOTE, 'Oracle', '', createOracleConfig),
      DBMSDesc(self.DBMS_KEYS_LIST[2], STORAGE_TYPE_REMOTE, 'MySQL', '', createMySQLConfig),
      DBMSDesc(self.DBMS_KEYS_LIST[3], STORAGE_TYPE_REMOTE, 'PostgreSQL', '', createPGConfig),
      DBMSDesc(self.DBMS_KEYS_LIST[4], STORAGE_TYPE_REMOTE, 'Microsoft SQL Server', 'Tech Preview', createMSSQLConfig),
      DBMSDesc(self.DBMS_KEYS_LIST[5], STORAGE_TYPE_REMOTE, 'SQL Anywhere', '', createSQLAConfig)
    ]

    self.DBMS_DICT = \
    {
      '-': 0,
      '-' + STORAGE_TYPE_LOCAL: 0,
      self.DBMS_KEYS_LIST[0] + '-': 0,
      self.DBMS_KEYS_LIST[2] + '-': 2,
      self.DBMS_KEYS_LIST[2] + '-' + STORAGE_TYPE_REMOTE: 2,
      self.DBMS_KEYS_LIST[4] + '-': 4,
      self.DBMS_KEYS_LIST[4] + '-' + STORAGE_TYPE_REMOTE: 4,
      self.DBMS_KEYS_LIST[1] + '-': 1,
      self.DBMS_KEYS_LIST[1] + '-' + STORAGE_TYPE_REMOTE: 1,
      self.DBMS_KEYS_LIST[3] + '-': 3,
      self.DBMS_KEYS_LIST[3] + '-' + STORAGE_TYPE_LOCAL: 0,
      self.DBMS_KEYS_LIST[3] + '-' + STORAGE_TYPE_REMOTE: 3,
      self.DBMS_KEYS_LIST[5] + '-': 5,
      self.DBMS_KEYS_LIST[5] + '-' + STORAGE_TYPE_REMOTE: 5,
    }

    self.DBMS_PROMPT_PATTERN = "[{0}] - {1}{2}\n"
    self.DBMS_CHOICE_PROMPT_PATTERN = "==============================================================================\n" \
                                      "Enter choice ({0}): "
    self.JDK_VALID_CHOICES_PATTERN = "^[{0}]$"

  def force_dbms_setup(self):
    dbms_name = self.get_default_dbms_name()
    if dbms_name.strip():
      return True
    else:
      return False

  def select_dbms(self, options):
    try:
      dbms_index = options.database_index
    except AttributeError:
      dbms_index = self._get_default_dbms_index(options)

    if options.must_set_database_options:
      n_dbms = 1
      dbms_choice_prompt = "==============================================================================\n" \
                           "Choose one of the following options:\n"
      dbms_choices = ''
      for desc in self.DBMS_LIST:
        if len(desc.storage_name) > 0:
          dbms_storage = " ({0})".format(desc.storage_name)
        else:
          dbms_storage = ""
        dbms_choice_prompt += self.DBMS_PROMPT_PATTERN.format(n_dbms, desc.dbms_name, dbms_storage)
        dbms_choices += str(n_dbms)
        n_dbms += 1

      database_num = str(dbms_index + 1)
      dbms_choice_prompt += self.DBMS_CHOICE_PROMPT_PATTERN.format(database_num)
      dbms_valid_choices = self.JDK_VALID_CHOICES_PATTERN.format(dbms_choices)

      database_num = get_validated_string_input(
        dbms_choice_prompt,
        database_num,
        dbms_valid_choices,
        "Invalid number.",
        False
      )

      dbms_index = int(database_num) - 1
      if dbms_index >= n_dbms:
        print_info_msg('Unknown db option, default to {0} {1}.'.format(
          self.DBMS_LIST[0].storage_name, self.DBMS_LIST[0].dbms_name))
        dbms_index = 0

    return dbms_index

  def create(self, options, properties, dbId = "Ambari"):
    """
    # Linux implementation of the factory method. The outcome of the derived implementations
    #  is expected to be a subclass of DBMSConfig.
    # properties = property bag that will ultimately define the type of database. Supported types are
    #   MySQL, MSSQL, Oracle and PostgreSQL.
    # dbId = additional information, that helps distinguish between various database connections, if applicable
    """

    try:
      index = options.database_index
    except AttributeError:
      index = options.database_index = self._get_default_dbms_index(options)

    desc = self.DBMS_LIST[index]
    options.persistence_type = desc.storage_key
    dbmsConfig = desc.create_config(options, properties, dbId)
    return dbmsConfig

  def get_supported_dbms(self):
    return self.DBMS_KEYS_LIST

  def get_supported_jdbc_drivers(self):
    return self.DRIVER_KEYS_LIST

  def get_default_dbms_name(self):
    properties = get_ambari_properties()
    default_dbms_name = get_value_from_properties(properties, DEFAULT_DBMS_PROPERTY, "").strip().lower()
    if default_dbms_name not in self.DBMS_KEYS_LIST:
      return ""
    else:
      return default_dbms_name

  def _get_default_dbms_index(self, options):
    default_dbms_name = self.get_default_dbms_name()
    try:
      dbms_name = options.dbms
      if not dbms_name:
        dbms_name = default_dbms_name
    except AttributeError:
      dbms_name = default_dbms_name
    try:
      persistence_type = options.persistence_type
      if not persistence_type:
        persistence_type = ""
    except AttributeError:
      persistence_type = ""

    try:
      def_index = self.DBMS_DICT[dbms_name + "-" + persistence_type]
    except KeyError:
      # Unsupported database type (e.g. local Oracle, MySQL or MSSQL)
      raise FatalException(15, "Invalid database selection: {0} {1}".format(
          getattr(options, "persistence_type", ""), getattr(options, "dbms", "")))

    return def_index


def check_jdbc_drivers(args):
  # create jdbc symlinks if jdbc drivers are available in resources
  properties = get_ambari_properties()
  if properties == -1:
    err = "Error getting ambari properties"
    print_error_msg(err)
    raise FatalException(-1, err)

  resources_dir = get_resources_location(properties)

  try:
    db_idx_orig = args.database_index
  except AttributeError:
    db_idx_orig = None

  factory = DBMSConfigFactory()

  # AMBARI-5696 Validate the symlinks for each supported driver, in case various back-end HDP services happen to
  #  use different DBMSes
  # This is skipped on Windows
  db_idx = 1

  try:
    while db_idx < len(factory.get_supported_dbms()):
      args.database_index = db_idx
      dbms = factory.create(args, properties)
      if dbms.driver_symlink_name:
        jdbc_file_path = os.path.join(resources_dir, dbms.driver_file_name)
        if os.path.isfile(jdbc_file_path):
          jdbc_symlink = os.path.join(resources_dir, dbms.driver_symlink_name)
          if os.path.lexists(jdbc_symlink):
            os.remove(jdbc_symlink)
          os.symlink(jdbc_file_path, jdbc_symlink)
      db_idx += 1
  finally:
    args.database_index = db_idx_orig


#Check the JDBC driver status
#If not found abort
#Get SQL Server service status from SCM
#If 'stopped' then start it
#Wait until the status is 'started' or a configured timeout elapses
#If the timeout has been reached, bail out with exception
def ensure_dbms_is_running(options, properties, scmStatus=None):
  factory = DBMSConfigFactory()
  dbms = factory.create(options, properties)
  result = dbms._is_jdbc_driver_installed(properties)
  if result == -1:
    raise FatalException(-1, "JDBC driver is not installed. Run ambari-server setup and try again.")
  dbms.ensure_dbms_is_running(options, properties, scmStatus)


def ensure_jdbc_driver_is_installed(options, properties):
  factory = DBMSConfigFactory()
  dbms = factory.create(options, properties)
  result = dbms._is_jdbc_driver_installed(properties)
  if result == -1:
    raise FatalException(-1, dbms.JDBC_DRIVER_INSTALL_MSG)
  dbms._extract_client_tarball(properties)

def get_native_libs_path(options, properties):
  factory = DBMSConfigFactory()
  dbms = factory.create(options, properties)
  return dbms._get_native_libs(properties)

def get_jdbc_driver_path(options, properties):
  factory = DBMSConfigFactory()
  dbms = factory.create(options, properties)
  return dbms._get_default_driver_path(properties)


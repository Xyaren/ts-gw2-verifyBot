#!/usr/bin/python
import binascii  # crc32
import datetime  # for date strings
import json
import logging
import os  # operating system commands -check if files exist
import re
import sqlite3  # Database
import traceback
import urllib.parse  # for fetching guild emblems urls
from threading import RLock

import requests  # to download guild emblems
import schedule  # Allows auditing of users every X days

from bot import Config, TS3Auth, ts
from bot.ts.TS3Facade import TS3Facade
from bot.ts.ThreadsafeTSConnection import ThreadsafeTSConnection, default_exception_handler, signal_exception_handler
from bot.util.StringShortener import StringShortener

LOG = logging.getLogger(__name__)


def request(url):
    response = requests.get(url, headers={"Content-Type": "application/json"})

    if response.status_code == 200:
        return json.loads(response.content.decode("utf-8"))
    else:
        return None


#######################################


class ThreadsafeDBConnection(object):
    def __init__(self, db_name):
        self.db_name = db_name
        self.conn = sqlite3.connect(db_name, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
        self.cursor = self.conn.cursor()
        self.lock = RLock()


class Bot:

    @property
    def ts_connection(self):
        return self._ts_connection

    def __init__(self, db, ts_connection: ThreadsafeTSConnection, ts_repository: TS3Facade, verified_group, bot_nickname="TS3BOT"):
        self._ts_repository: TS3Facade = ts_repository
        self._ts_connection: ThreadsafeTSConnection = ts_connection
        admin_data, ex = self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("whoami").first())
        self.db_name = db
        self.name = admin_data.get('client_login_name')
        self.client_id = admin_data.get('client_id')
        self.nickname = self.ts_connection.forceRename(bot_nickname)
        self.verified_group = verified_group
        self.vgrp_id = self.groupFind(verified_group)
        self.getUserDatabase()
        self.c_audit_date = datetime.date.today()
        self.commander_checker = CommanderChecker(self, Config.poll_group_names)

    # Helps find the group ID for a group name
    def groupFind(self, group_to_find):
        self.groups_list, ex = self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("servergrouplist").all())
        for group in self.groups_list:
            if group.get('name') == group_to_find:
                return group.get('sgid')
        return -1

    def clientNeedsVerify(self, unique_client_id):
        client_db_id = self.getTsDatabaseID(unique_client_id)

        # Check if user is in verified group
        if any(perm_grp.get('name') == self.verified_group for perm_grp in
               self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("servergroupsbyclientid", cldbid=client_db_id).all())[0]):
            return False  # User already verified

        # Check if user is authenticated in database and if so, re-adds them to the group
        with self.dbc.lock:
            current_entries = self.dbc.cursor.execute("SELECT * FROM users WHERE ts_db_id=?", (unique_client_id,)).fetchall()
            if len(current_entries) > 0:
                self.setPermissions(unique_client_id)
                return False

        return True  # User not verified

    def setPermissions(self, unique_client_id):
        try:
            client_db_id = self.getTsDatabaseID(unique_client_id)
            LOG.debug("Adding Permissions: CLUID [%s] SGID: %s   CLDBID: %s", unique_client_id, self.vgrp_id, client_db_id)
            # Add user to group
            _, ex = self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupaddclient", sgid=self.vgrp_id, cldbid=client_db_id))
            if ex:
                LOG.error("Unable to add client to '%s' group. Does the group exist?", self.verified_group)
        except ts.query.TS3QueryError as err:
            LOG.error("Setting permissions failed: %s", err)  # likely due to bad client id

    def removePermissions(self, unique_client_id):
        try:
            client_db_id = self.getTsDatabaseID(unique_client_id)
            LOG.debug("Removing Permissions: CLUID [%s] SGID: %s   CLDBID: %s", unique_client_id, self.vgrp_id, client_db_id)

            # Remove user from group
            _, ex = self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupdelclient", sgid=self.vgrp_id, cldbid=client_db_id), signal_exception_handler)
            if ex:
                LOG.error("Unable to remove client from '%s' group. Does the group exist and are they member of the group?", self.verified_group)
            # Remove users from all groups, except the whitelisted ones
            if Config.purge_completely:
                # FIXME: remove channel groups as well
                assigned_groups, ex = self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("servergroupsbyclientid", cldbid=client_db_id).all())
                if assigned_groups is not None:
                    for g in assigned_groups:
                        if g.get("name") not in Config.purge_whitelist:
                            self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupdelclient", sgid=g.get("sgid"), cldbid=client_db_id), lambda ex: None)
        except ts.query.TS3QueryError as err:
            LOG.error("Removing permissions failed: %s", err)  # likely due to bad client id

    def removePermissionsByGW2Account(self, gw2account):
        with self.dbc.lock:
            tsDbIds = self.dbc.cursor.execute("SELECT ts_db_id FROM users WHERE account_name = ?", (gw2account,)).fetchall()
            for tdi, in tsDbIds:
                self.removePermissions(tdi)
                LOG.debug("Removed permissions from %s", tdi)
            self.dbc.cursor.execute("DELETE FROM users WHERE account_name = ?", (gw2account,))
            changes = self.dbc.cursor.execute("SELECT changes()").fetchone()[0]
            self.dbc.conn.commit()
            return changes

    def getUserDBEntry(self, client_unique_id):
        """
        Retrieves the DB entry for a unique client ID.
        Is either a dictionary of database-field-names to values, or None if no such entry was found in the DB.
        """
        with self.dbc.lock:
            entry = self.dbc.cursor.execute("SELECT * FROM users WHERE ts_db_id=?", (client_unique_id,)).fetchall()
            if len(entry) < 1:
                # user not registered
                return None
            entry = entry[0]
            keys = self.dbc.cursor.description
            assert len(entry) == len(keys)
            return dict([(keys[i][0], entry[i]) for i in range(len(entry))])

    def getUserDatabase(self):
        if os.path.isfile(self.db_name):
            self.dbc = ThreadsafeDBConnection(self.db_name)  # sqlite3.connect(self.db_name, check_same_thread = False, detect_types = sqlite3.PARSE_DECLTYPES)
            LOG.info("Loaded User Database...")
        else:
            self.dbc = ThreadsafeDBConnection(self.db_name)  # sqlite3.connect(self.db_name, check_same_thread = False, detect_types = sqlite3.PARSE_DECLTYPES)
            # self.dbc.cursor = self.dbc.conn.cursor()
            LOG.info("No User Database found...created new database!")
            with self.dbc.lock:
                # USERS
                self.dbc.cursor.execute("CREATE TABLE users(ts_db_id text primary key, account_name text, api_key text, created_date date, last_audit_date date)")
                # BOT INFO
                self.dbc.cursor.execute("CREATE TABLE bot_info(version text, last_succesful_audit date)")
                self.dbc.conn.commit()
                self.dbc.cursor.execute('INSERT INTO bot_info (version, last_succesful_audit) VALUES (?,?)', (Config.current_version, datetime.date.today(),))
                self.dbc.conn.commit()
                # GUILD INFO
                self.dbc.cursor.execute('''CREATE TABLE guilds(
                                guild_id integer primary key autoincrement,
                                guild_name text UNIQUE,
                                ts_group text UNIQUE)''')
                self.dbc.conn.commit()

                # GUILD IGNORES
                self.dbc.cursor.execute('''CREATE TABLE guild_ignores(
                                guild_ignore_id integer primary key autoincrement,
                                guild_id integer,
                                ts_db_id text,
                                ts_name text,
                                FOREIGN KEY(guild_id) REFERENCES guilds(guild_id),
                                UNIQUE(guild_id, ts_db_id))''')
                self.dbc.conn.commit()

    def TsClientLimitReached(self, gw_acct_name):
        with self.dbc.lock:
            current_entries = self.dbc.cursor.execute("SELECT * FROM users WHERE account_name=?", (gw_acct_name,)).fetchall()
            return len(current_entries) >= Config.client_restriction_limit

    def addUserToDB(self, client_unique_id, account_name, api_key, created_date, last_audit_date):
        with self.dbc.lock:
            # client_id = self.getActiveTsUserID(client_unique_id)
            client_exists = self.dbc.cursor.execute("SELECT * FROM users WHERE ts_db_id=?", (client_unique_id,)).fetchall()
            if len(client_exists) > 1:
                LOG.warning("Found multiple database entries for single unique teamspeakid %s.", client_unique_id)
            if len(client_exists) != 0:  # If client TS database id is in BOT's database.
                self.dbc.cursor.execute("""UPDATE users SET ts_db_id=?, account_name=?, api_key=?, created_date=?, last_audit_date=? WHERE ts_db_id=?""",
                                        (client_unique_id, account_name, api_key, created_date, last_audit_date, client_unique_id))
                LOG.info("Teamspeak ID %s already in Database updating with new Account Name '%s'. (likely permissions changed by a Teamspeak Admin)", client_unique_id, account_name)
            else:
                self.dbc.cursor.execute("INSERT INTO users ( ts_db_id, account_name, api_key, created_date, last_audit_date) VALUES(?,?,?,?,?)",
                                        (client_unique_id, account_name, api_key, created_date, last_audit_date))
            self.dbc.conn.commit()

    def removeUserFromDB(self, client_db_id):
        with self.dbc.lock:
            self.dbc.cursor.execute("DELETE FROM users WHERE ts_db_id=?", (client_db_id,))
            self.dbc.conn.commit()

    # def updateGuildTags(self, client_db_id, auth):
    def updateGuildTags(self, user, auth):
        if auth.guilds_error:
            LOG.error("Did not update guild groups for player '%s', as loading the guild groups caused an error.", auth.name)
            return
        uid = user.unique_id  # self.getTsUniqueID(client_db_id)
        client_db_id = user.ts_db_id
        ts_groups = {sg.get("name"): sg.get("sgid") for sg in self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("servergrouplist").all())[0]}
        ingame_member_of = set(auth.guild_names)
        # names of all groups the user is in, not just guild ones
        current_group_names = []
        try:
            current_group_names = [g.get("name") for g in
                                   self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("servergroupsbyclientid", cldbid=client_db_id).all(), signal_exception_handler)[0]]
        except TypeError:
            # user had no groups (results in None, instead of an empty list) -> just stick with the []
            pass

        # data of all guild groups the user is in
        param = ",".join(["'%s'" % (cgn.replace('"', '\\"').replace("'", "\\'"),) for cgn in current_group_names])
        # sanitisation is restricted to replacing single and double quotes. This should not be that much of a problem, since
        # the input for the parameters here are the names of our own server groups on our TS server.
        current_guild_groups = []
        hidden_groups = {}
        with self.dbc.lock:
            current_guild_groups = self.dbc.cursor.execute("SELECT ts_group, guild_name FROM guilds WHERE ts_group IN (%s)" % (param,)).fetchall()
            # groups the user doesn't want to wear
            hidden_groups = set([g[0] for g in self.dbc.cursor.execute("SELECT g.ts_group FROM guild_ignores AS gi JOIN guilds AS g ON gi.guild_id = g.guild_id  WHERE ts_db_id = ?", (uid,))])
        # REMOVE STALE GROUPS
        for ggroup, gname in current_guild_groups:
            if ggroup in hidden_groups:
                LOG.info("Player %s chose to hide group '%s', which is now removed.", auth.name, ggroup)
                self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupdelclient", sgid=ts_groups[ggroup], cldbid=client_db_id))
            elif gname not in ingame_member_of:
                if ggroup not in ts_groups:
                    LOG.warning(
                        "Player %s should be removed from the TS group '%s' because they are not a member of guild '%s'."
                        " But no matching group exists."
                        " You should remove the entry for this guild from the db or check the spelling of the TS group in the DB. Skipping.",
                        ggroup, auth.name, gname)
                else:
                    LOG.info("Player %s is no longer part of the guild '%s'. Removing attached group '%s'.", auth.name, gname, ggroup)
                    self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupdelclient", sgid=ts_groups[ggroup], cldbid=client_db_id))

        # ADD DUE GROUPS
        for g in ingame_member_of:
            ts_group = None
            with self.dbc.lock:
                ts_group = self.dbc.cursor.execute("SELECT ts_group FROM guilds WHERE guild_name = ?", (g,)).fetchone()
            if ts_group:
                ts_group = ts_group[0]  # first and only column, if a row exists
                if ts_group not in current_group_names:
                    if ts_group in hidden_groups:
                        LOG.info("Player %s is entitled to TS group '%s', but chose to hide it. Skipping.", auth.name, ts_group)
                    else:
                        if ts_group not in ts_groups:
                            LOG.warning(
                                "Player %s should be assigned the TS group '%s' because they are member of guild '%s'."
                                " But the group does not exist. You should remove the entry for this guild from the db or create the group."
                                " Skipping.",
                                auth.name, ts_group, g)
                        else:
                            LOG.info("Player %s is member of guild '%s' and will be assigned the TS group '%s'.", auth.name, g, ts_group)
                            self.ts_connection.ts3exec(lambda ts_connection: ts_connection.exec_("servergroupaddclient", sgid=ts_groups[ts_group], cldbid=client_db_id))

    def auditUsers(self):
        LOG.info("Auditing users")
        import threading
        threading.Thread(target=self._auditUsers).start()

    def _auditUsers(self):
        self.c_audit_date = datetime.date.today()  # Update current date everytime run
        self.db_audit_list = []
        with self.dbc.lock:
            self.db_audit_list = self.dbc.cursor.execute('SELECT * FROM users').fetchall()
        for audit_user in self.db_audit_list:

            # Convert to single variables
            audit_ts_id = audit_user[0]
            audit_account_name = audit_user[1]
            audit_api_key = audit_user[2]
            # audit_created_date = audit_user[3]
            audit_last_audit_date = audit_user[4]

            LOG.debug("Audit: User %s", audit_account_name)
            LOG.debug("TODAY |%s|  NEXT AUDIT |%s|", self.c_audit_date, audit_last_audit_date + datetime.timedelta(days=Config.audit_period))

            # compare audit date
            if self.c_audit_date >= audit_last_audit_date + datetime.timedelta(days=Config.audit_period):
                LOG.info("User %s is due for auditing!", audit_account_name)
                auth = TS3Auth.AuthRequest(audit_api_key, audit_account_name)
                if auth.success:
                    LOG.info("User %s is still on %s. Succesful audit!", audit_account_name, auth.world.get("name"))
                    # self.getTsDatabaseID(audit_ts_id)
                    self.updateGuildTags(User(self.ts_connection, unique_id=audit_ts_id), auth)
                    with self.dbc.lock:
                        self.dbc.cursor.execute("UPDATE users SET last_audit_date = ? WHERE ts_db_id= ?", (self.c_audit_date, audit_ts_id,))
                        self.dbc.conn.commit()
                else:
                    LOG.info("User %s is no longer on our server. Removing access....", audit_account_name)
                    self.removePermissions(audit_ts_id)
                    self.removeUserFromDB(audit_ts_id)

        with self.dbc.lock:
            self.dbc.cursor.execute('INSERT INTO bot_info (last_succesful_audit) VALUES (?)', (self.c_audit_date,))
            self.dbc.conn.commit()

    def broadcastMessage(self):
        broadcast_message = Config.locale.get("bot_msg_broadcast")
        self._ts_repository.send_text_message_to_current_channel(msg=broadcast_message)

    def getActiveTsUserID(self, client_unique_id):
        return self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("clientgetids", cluid=client_unique_id).first().get('clid'))[0]

    def getTsDatabaseID(self, client_unique_id):
        return self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("clientgetdbidfromuid", cluid=client_unique_id).first().get('cldbid'))[0]

    def getTsUniqueID(self, client_db_id):
        return self.ts_connection.ts3exec(lambda ts_connection: ts_connection.query("clientgetnamefromdbid", cldbid=client_db_id).first().get('cluid'))[0]

    def loginEventHandler(self, event):
        # raw_sgroups = event.parsed[0].get('client_servergroups')
        raw_clid = event.parsed[0].get('clid')
        raw_cluid = event.parsed[0].get('client_unique_identifier')

        if raw_clid == self.client_id:
            return

        if self.clientNeedsVerify(raw_cluid):
            self._ts_repository.send_text_message_to_client(raw_clid, Config.locale.get("bot_msg_verify"))

    def commandCheck(self, command_string):
        action = (None, None)
        for allowed_cmd in Config.cmd_list:
            if re.match(r'(^%s)\s*' % (allowed_cmd,), command_string):
                toks = command_string.split()  # no argument for split() splits on arbitrary whitespace
                action = (toks[0], toks[1:])
        return action

    def try_get(self, dictionary, key, lower=False, typer=lambda x: x, default=None):
        v = typer(dictionary[key] if key in dictionary else default)
        return v.lower() if lower and isinstance(v, str) else v

    def setResetroster(self, ts3conn, date, red=[], green=[], blue=[], ebg=[]):
        leads = ([], red, green, blue, ebg)  # keep RGB order! EBG as last! Pad first slot (header) with empty list

        channels = [(p, c.replace("$DATE", date)) for p, c in Config.reset_channels]
        for i in range(len(channels)):
            pattern, clean = channels[i]
            lead = leads[i]

            TS3_MAX_SIZE_CHANNEL_NAME = 40
            shortened = StringShortener(TS3_MAX_SIZE_CHANNEL_NAME - len(clean)).shorten(lead)
            newname = "%s%s" % (clean, ", ".join(shortened))

            channel = self._ts_repository.channel_find(pattern)
            if channel is None:
                LOG.warning("No channel found with pattern '%s'. Skipping.", pattern)
                return

            _, ts3qe = ts3conn.ts3exec(lambda tsc: tsc.exec_("channeledit", cid=channel.channel_id, channel_name=newname), signal_exception_handler)
            if ts3qe is not None and ts3qe.resp.error["id"] == "771":
                # channel name already in use
                # probably not a bug (channel still unused), but can be a config problem
                LOG.info("Channel '%s' already exists. This is probably not a problem. Skipping.", newname)
        return 0

    def getGuildInfo(self, guildname):
        """
        Lookup guild by name. If such a guild exists (and the API is available)
        the info as specified on https://wiki.guildwars2.com/wiki/API:2/guild/:id is returned.
        Else, None is returned.
        """
        ids = request("https://api.guildwars2.com/v2/guild/search?name=%s" % (urllib.parse.quote(guildname),))
        return None if ids is None or len(ids) == 0 else request("https://api.guildwars2.com/v2/guild/%s" % (ids[0]))

    def getActiveCommanders(self):
        return self.commander_checker.execute()

    def removeGuild(self, name):
        """
        Removes a guild from the TS. That is:
        - deletes their guild channel and all their subchannels by force
        - removes the group from TS by force
        - remove the auto-assignment for that group from the DB

        name: name of the guild as in the game
        """
        SUCCESS = 0
        INVALID_GUILD_NAME = 1
        NO_DB_ENTRY = 2
        INVALID_PARAMETERS = 5

        if name is None:
            return INVALID_PARAMETERS

        ginfo = self.getGuildInfo(name)
        if ginfo is None:
            return INVALID_GUILD_NAME

        with self.dbc.lock:
            g = self.dbc.cursor.execute("SELECT ts_group FROM guilds WHERE guild_name = ?", (name,)).fetchone()
            groupname = g[0] if g is not None else None

        if groupname is None:
            return NO_DB_ENTRY

        ts3conn = self.ts_connection
        tag = ginfo.get("tag")

        # FROM DB
        LOG.debug("Deleting guild '%s' from DB.", name)
        with self.dbc.lock:
            self.dbc.cursor.execute("DELETE FROM guilds WHERE guild_name = ?", (name,))
            self.dbc.conn.commit()

        # CHANNEL
        channelname = "%s [%s]" % (name, tag)
        channel = self._ts_repository.channel_find(channelname)
        if channel is None:
            LOG.debug("No channel '%s' to delete.", channelname)
        else:
            LOG.debug("Deleting channel '%s'.", channelname)
            ts3conn.ts3exec(lambda tsc: tsc.exec_("channeldelete", cid=channel.channel_id, force=1))

        # GROUP
        groups, ex = ts3conn.ts3exec(lambda tsc: tsc.query("servergrouplist").all())
        group = next((g for g in groups if g.get("name") == groupname), None)
        if group is None:
            LOG.debug("No group '%s' to delete.", groupname)
        else:
            LOG.debug("Deleting group '%s'.", groupname)
            ts3conn.ts3exec(lambda tsc: tsc.exec_("servergroupdel", sgid=group.get("sgid"), force=1))

        return SUCCESS

    def createGuild(self, name, tag, groupname, contacts):
        """
        Creates a guild in the TS.
        - retrieves and uploads their emblem as icon
        - creates guild channel with subchannels as read from the config with the icon
        - creates a guild group with the icon and appropriate permissions
        - adds in automatic assignment of the guild group upon re-verification
        - adds the contact persons as initial channel description
        - gives the contact role to the contact persons if they can be found in the DB

        name: name of the guild as is seen ingame
        tag: their tag
        groupname: group that should be used for them. Useful if the tag is already taken
        contacts: list of account names (Foo.1234) that should be noted down as contact and receive the special role for the new channel
        returns: 0 for success or an error code indicating the problem (see below)
        """
        SUCCESS = 0
        DUPLICATE_TS_GROUP = 1
        DUPLICATE_DB_ENTRY = 2
        DUPLICATE_TS_CHANNEL = 3
        MISSING_PARENT_CHANNEL = 4
        INVALID_PARAMETERS = 5

        if (name is None or tag is None or groupname is None or contacts is None
                or len(name) < 3 or len(tag) < 2 or len(groupname) < 2
                or not isinstance(contacts, list)):
            return INVALID_PARAMETERS

        ts3conn = self.ts_connection
        channelname = "%s [%s]" % (name, tag)

        channel_description = self.create_guild_channel_description(contacts, name, tag)

        LOG.info("Creating guild '%s' with tag '%s', guild group '%s', and contacts '%s'." % (name, tag, groupname, ", ".join(contacts)))

        # lock for the whole block to avoid constant interference
        # locking the ts3conn is vital to properly do the TS3FileTransfer
        # down the line.
        with ts3conn.lock, self.dbc.lock:
            #############################################
            # CHECK IF GROUPS OR CHANNELS ALREADY EXIST #
            #############################################
            LOG.debug("Doing preliminary checks.")
            groups, ex = ts3conn.ts3exec(lambda tsc: tsc.query("servergrouplist").all(), default_exception_handler)
            group = next((g for g in groups if g.get("name") == groupname), None)
            if group is not None:
                # group already exists!
                LOG.debug("Can not create a group '%s', because it already exists. Aborting guild creation.", group)
                return DUPLICATE_TS_GROUP

            with self.dbc.lock:
                dbgroups = self.dbc.cursor.execute("SELECT ts_group, guild_name FROM guilds WHERE ts_group = ?", (groupname,)).fetchall()
                if len(dbgroups) > 0:
                    LOG.debug("Can not create a DB entry for TS group '%s', as it already exists. Aborting guild creation.", groupname)
                    return DUPLICATE_DB_ENTRY

            channel = self._ts_repository.channel_find(channelname)
            if channel is not None:
                # channel already exists!
                LOG.debug("Can not create a channel '%s', as it already exists. Aborting guild creation.", channelname)
                return DUPLICATE_TS_CHANNEL

            parent = self._ts_repository.channel_find(Config.guilds_parent_channel)
            if parent is None:
                # parent channel does not exist!
                LOG.debug("Can not find a parent-channel '%s' for guilds. Aborting guild creation.", Config.guilds_parent_channel)
                return MISSING_PARENT_CHANNEL

            LOG.debug("Checks complete.")

            # Icon uploading
            icon_id = self._handle_guild_icon(name, ts3conn)  # Returns None if no icon

            ##################################
            # CREATE CHANNEL AND SUBCHANNELS #
            ##################################
            LOG.debug("Creating guild channels...")
            pid = parent.get("cid")
            info, ex = ts3conn.ts3exec(lambda tsc: tsc.query("channelinfo", cid=pid).all(), signal_exception_handler)
            # assert channel and group both exist and parent channel is available
            all_guild_channels = [c for c in ts3conn.ts3exec(lambda tc: tc.query("channellist").all(), signal_exception_handler)[0] if c.get("pid") == pid]
            all_guild_channels.sort(key=lambda c: c.get("channel_name"), reverse=True)

            # Assuming the channels are already in order on the server,
            # find the first channel whose name is alphabetically smaller than the new channel name.
            # The sort_order of channels actually specifies after which channel they should be
            # inserted. Giving 0 as sort_order puts them in first place after the parent.
            found_place = False
            sort_order = 0
            i = 0
            while i < len(all_guild_channels) and not found_place:
                if all_guild_channels[i].get("channel_name") > channelname:
                    i += 1
                else:
                    sort_order = int(all_guild_channels[i].get("cid"))
                    found_place = True

            cinfo, ex = ts3conn.ts3exec(lambda tsc: tsc.query("channelcreate",
                                                              channel_name=channelname,
                                                              channel_description=channel_description,
                                                              cpid=pid,
                                                              channel_flag_permanent=1,
                                                              channel_maxclients=0,
                                                              channel_order=sort_order,
                                                              channel_flag_maxclients_unlimited=0)
                                        .first(), signal_exception_handler)

            guild_channel_perms = [
                ("i_channel_needed_join_power", 25),
                ("i_channel_needed_subscribe_power", 25),
                ("i_channel_needed_modify_power", 45),
                ("i_channel_needed_delete_power", 75)
            ]

            perms = guild_channel_perms.copy()
            if icon_id is not None:
                perms.append(("i_icon_id", icon_id))

            def channelApplyPermissions(cid, perms):
                for p, v in perms:
                    _, ex = ts3conn.ts3exec(lambda tsc: tsc.exec_("channeladdperm", cid=cid, permsid=p, permvalue=v, permnegated=0, permskip=0),
                                            signal_exception_handler)

            channelApplyPermissions(cinfo.get("cid"), perms)

            for c in Config.guild_sub_channels:
                # FIXME: error check
                res, ex = ts3conn.ts3exec(lambda tsc: tsc.query("channelcreate", channel_name=c, cpid=cinfo.get("cid"), channel_flag_permanent=1)
                                          .first(), signal_exception_handler)
                channelApplyPermissions(res.get("cid"), guild_channel_perms)

        ###################
        # CREATE DB GROUP #
        ###################
        # must exist in DB before creating group to have it available when reordering groups.
        LOG.debug("Creating entry in database for auto assignment of guild group...")
        with self.dbc.lock:
            self.dbc.cursor.execute("INSERT INTO guilds(ts_group, guild_name) VALUES(?,?)", (groupname, name))
            self.dbc.conn.commit()

        #######################
        # CREATE SERVER GROUP #
        #######################
        LOG.debug("Creating and configuring server group...")
        resp, ex = ts3conn.ts3exec(lambda tsc: tsc.query("servergroupadd", name=groupname).first(), signal_exception_handler)
        guildgroupid = resp.get("sgid")
        if ex is not None and ex.resp.error["id"] == "1282":
            LOG.warning("Duplication error while trying to create the group '%s' for the guild %s [%s]." % (groupname, name, tag))

        def servergroupaddperm(sgid, permsid, permvalue):
            return ts3conn.ts3exec(lambda tsc: tsc.exec_("servergroupaddperm",
                                                         sgid=sgid,
                                                         permsid=permsid,
                                                         permvalue=permvalue,
                                                         permnegated=0,
                                                         permskip=0),
                                   signal_exception_handler)

        perms = [
            ("b_group_is_permanent", 1),
            ("i_group_show_name_in_tree", 1),
            ("i_group_needed_modify_power", 75),
            ("i_group_needed_member_add_power", 50),
            ("i_group_needed_member_remove_power", 50),
            ("i_group_sort_id", Config.guilds_sort_id),
        ]

        if icon_id is not None:
            perms.append(("i_icon_id", icon_id))

        for p, v in perms:
            x, ex = servergroupaddperm(guildgroupid, p, v)

        groups.append({"sgid": resp.get("sgid"), "name": groupname})  # the newly created group has to be added to properly iterate over the guild groups
        guildgroups = []
        with self.dbc.lock:
            guildgroups = [g[0] for g in self.dbc.cursor.execute("SELECT ts_group FROM guilds ORDER BY ts_group").fetchall()]
        for i in range(len(guildgroups)):
            g = next((g for g in groups if g.get("name") == guildgroups[i]), None)
            if g is None:
                # error! Group deleted from TS, but not from DB!
                LOG.warning("Found guild '%s' in the database, but no coresponding server group! Skipping this entry, but it should be fixed!", guildgroups[i])
            else:
                tp = Config.guilds_maximum_talk_power - i

                if tp < 0:
                    LOG.warning("Talk power for guild %s is below 0.", g.get("name"))

                # sort guild groups to have users grouped by their guild tag alphabetically in channels
                x, ex = servergroupaddperm(g.get("sgid"), "i_client_talk_power", tp)

        ################
        # ADD CONTACTS #
        ################
        LOG.debug("Adding contacts...")
        cgroups, ex = ts3conn.ts3exec(lambda tsc: tsc.query("channelgrouplist").all(), default_exception_handler)
        contactgroup = next((cg for cg in cgroups if cg.get("name") == Config.guild_contact_channel_group), None)
        if contactgroup is None:
            LOG.debug("Can not find a group '%s' for guild contacts. Skipping.", contactgroup)
        else:
            for c in contacts:
                with self.dbc.lock:
                    accs = [row[0] for row in self.dbc.cursor.execute("SELECT ts_db_id FROM users WHERE lower(account_name) = lower(?)", (c,)).fetchall()]
                    for a in accs:
                        errored = False
                        try:
                            u = User(ts3conn, unique_id=a, ex_hand=signal_exception_handler)
                            tsdbid = u.ts_db_id
                            _, ex = ts3conn.ts3exec(lambda tsc: tsc.exec_("setclientchannelgroup", cid=cinfo.get("cid"), cldbid=tsdbid, cgid=contactgroup.get("cgid")),
                                                    signal_exception_handler)
                            # while we are at it, add the contacts to the guild group as well
                            _, ex2 = ts3conn.ts3exec(lambda tsc: tsc.exec_("servergroupaddclient", sgid=guildgroupid, cldbid=tsdbid), signal_exception_handler)

                            errored = ex is not None
                        except Exception:
                            errored = True
                        if errored:
                            LOG.error("Could not assign contact role '%s' to user '%s' with DB-unique-ID '%s' in "
                                      "guild channel for %s. Maybe the uid is not valid anymore.",
                                      Config.guild_contact_channel_group, c, a, name)
        return SUCCESS

    def _handle_guild_icon(self, name, ts3conn):
        #########################################
        # RETRIEVE AND UPLOAD GUILD EMBLEM ICON #
        #########################################
        LOG.debug("Retrieving and uploading guild emblem as icon from gw2mists...")
        icon_url = "https://api.gw2mists.de/guilds/emblem/%s/50.svg" % (urllib.parse.quote(name),)
        icon = requests.get(icon_url)

        # funnily enough, giving an invalid guild (or one that has no emblem)
        # results in HTTP 200, but a JSON explaining the error instead of an SVG image.
        # Storing this JSON and uploading it to TS just fails silently without
        # causing any problems!
        # Therefore checking content length..
        if len(icon.content) > 0:
            icon_id = binascii.crc32(name.encode('utf8'))

            icon_local_file_name = "%s_icon.svg" % (urllib.parse.quote(name),)  # using name instead of tag, because tags are not unique
            icon_server_path = "/icon_%s" % (icon_id,)
            self.upload_icon(icon, icon_local_file_name, icon_server_path, ts3conn)
            return icon_id
        else:
            LOG.debug("Empty Response. Guild probably has no icon. Skipping Icon upload.")
            return None

    def upload_icon(self, icon, icon_file_name, icon_server_path, ts3conn):
        def _ts_file_upload_hook(c: ts.response.TS3QueryResponse):
            if (c is not None) and (c.parsed is not None) \
                    and (len(c.parsed) == 1) and (c.parsed[0] is not None) \
                    and "msg" in c.parsed[0].keys() and c.parsed[0]["msg"] == "invalid size":
                from bot.ts import TS3UploadError
                raise TS3UploadError(0, "The uploaded Icon is too large")
            return None

        with open(icon_file_name, "w+b") as fh:
            try:
                # svg
                fh.write(icon.content)
                fh.flush()
                fh.seek(0)

                # it is important to have acquired the lock for the ts3conn globally
                # at this point, as we directly pass the wrapped connection around
                upload = ts.filetransfer.TS3FileTransfer(ts3conn.ts_connection)
                _ = upload.init_upload(input_file=fh,
                                       name=icon_server_path,
                                       cid=0,
                                       query_resp_hook=lambda c: _ts_file_upload_hook(c))
                LOG.info(f"Icon {icon_file_name} uploaded as {icon_server_path}.")
            except ts.common.TS3Error as ts3error:
                LOG.error("Error Uploading icon {icon_file_name}.")
                LOG.error(ts3error)
            finally:
                fh.close()
                os.remove(icon_file_name)

    def create_guild_channel_description(self, contacts, name, tag):
        contacts = "\n".join(["    • %s" % c for c in contacts])
        text = (f"[center]\n"
                f"[img]https://api.gw2mists.de/guilds/emblem/{urllib.parse.quote(name)}/128.svg[/img]\n"
                f"[size=20]{name} - {tag}[/size]\n"
                f"[/center]\n"
                f"[hr]\n"
                f"[size=12]Contacts:[/size]\n"
                f"{contacts}\n"
                f"[hr]\n")
        return text

    def clientMessageHandler(self, ipcserver, clientsocket, message):
        mtype = self.try_get(message, "type", lower=True)
        mcommand = self.try_get(message, "command", lower=True)
        margs = self.try_get(message, "args", typer=lambda a: dict(a), default={})
        mid = self.try_get(message, "message_id", typer=lambda a: int(a), default=-1)

        LOG.debug("[%s] %s", mtype, mcommand)

        if mtype == "post":
            # POST commands
            if mcommand == "setresetroster":
                mdate = self.try_get(margs, "date", default="dd.mm.yyyy")
                mred = self.try_get(margs, "rbl", default=[])
                mgreen = self.try_get(margs, "gbl", default=[])
                mblue = self.try_get(margs, "bbl", default=[])
                mebg = self.try_get(margs, "ebg", default=[])
                self.setResetroster(ipcserver.ts_connection, mdate, mred, mgreen, mblue, mebg)
            if mcommand == "createguild":
                mname = self.try_get(margs, "name", default=None)
                mtag = self.try_get(margs, "tag", default=None)
                mgroupname = self.try_get(margs, "tsgroup", default=mtag)
                mcontacts = self.try_get(margs, "contacts", default=[])
                res = -1 if mname is None or mtag is None else self.createGuild(mname, mtag, mgroupname, mcontacts)
                clientsocket.respond(mid, mcommand, {"status": res})

        if mtype == "delete":
            # DELETE commands
            if mcommand == "user":
                mgw2account = self.try_get(margs, "gw2account", default="")
                LOG.info("Received request to delete user '%s' from the TS registration database.", mgw2account)
                changes = self.removePermissionsByGW2Account(mgw2account)
                clientsocket.respond(mid, mcommand, {"deleted": changes})
            if mcommand == "guild":
                mname = self.try_get(margs, "name", default=None)
                LOG.info("Received request to delete guild %s", mname)
                res = self.removeGuild(mname)
                print(res)
                clientsocket.respond(mid, mcommand, {"status": res})

    # Handler that is used every time an event (message) is received from teamspeak server
    def messageEventHandler(self, event):
        """
        *event* is a ts3.response.TS3Event instance, that contains the name
        of the event and the data.
        """
        LOG.debug("event.event: %s", event.event)

        raw_cmd = event.parsed[0].get('msg')
        rec_from_name = event.parsed[0].get('invokername').encode('utf-8')  # fix any encoding issues introduced by Teamspeak
        rec_from_uid = event.parsed[0].get('invokeruid')
        rec_from_id = event.parsed[0].get('invokerid')
        rec_type = event.parsed[0].get('targetmode')

        if rec_from_id == self.client_id:
            return  # ignore our own messages.
        try:
            # Type 2 means it was channel text
            if rec_type == "2":
                cmd, args = self.commandCheck(raw_cmd)  # sanitize the commands but also restricts commands to a list of known allowed commands
                if cmd == "hideguild":
                    LOG.info("User '%s' wants to hide guild '%s'.", rec_from_name, args[0])
                    with self.dbc.lock:
                        try:
                            self.dbc.cursor.execute("INSERT INTO guild_ignores(guild_id, ts_db_id, ts_name) VALUES((SELECT guild_id FROM guilds WHERE ts_group = ?), ?,?)",
                                                    (args[0], rec_from_uid, rec_from_name))
                            self.dbc.conn.commit()
                            LOG.debug("Success!")
                            self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_hide_guild_success"))
                        except sqlite3.IntegrityError:
                            self.dbc.conn.rollback()
                            LOG.debug("Failed. The group probably doesn't exist or the user is already hiding that group.")
                            self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_hide_guild_unknown"))

                elif cmd == "unhideguild":
                    LOG.info("User '%s' wants to unhide guild '%s'.", rec_from_name, args[0])
                    with self.dbc.lock:
                        self.dbc.cursor.execute("DELETE FROM guild_ignores WHERE guild_id = (SELECT guild_id FROM guilds WHERE ts_group = ? AND ts_db_id = ?)", (args[0], rec_from_uid))
                        changes = self.dbc.cursor.execute("SELECT changes()").fetchone()[0]
                        self.dbc.conn.commit()
                        if changes > 0:
                            LOG.debug("Success!")
                            self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_unhide_guild_success"))
                        else:
                            LOG.debug("Failed. Either the guild is unknown or the user had not hidden the guild anyway.")
                            self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_unhide_guild_unknown"))
                elif cmd == 'verifyme':
                    return  # command disabled for now
                    # if self.clientNeedsVerify(rec_from_uid):
                    #     log.info("Verify Request Recieved from user '%s'. Sending PM now...\n        ...waiting for user response.", rec_from_name)
                    #     self.ts_connection.ts3exec(lambda tsc: tsc.exec_("sendtextmessage", targetmode = 1, target = rec_from_id, msg = Config.locale.get("bot_msg_verify")))
                    # else:
                    #     log.info("Verify Request Recieved from user '%s'. Already verified, notified user.", rec_from_name)
                    #     self.ts_connection.ts3exec(lambda tsc: tsc.exec_("sendtextmessage", targetmode = 1, target = rec_from_id, msg = Config.locale.get("bot_msg_alrdy_verified")))

            # Type 1 means it was a private message
            elif rec_type == '1':
                # reg_api_auth='\s*(\S+\s*\S+\.\d+)\s+(.*?-.*?-.*?-.*?-.*)\s*$'
                reg_api_auth = r'\s*(.*?-.*?-.*?-.*?-.*)\s*$'

                # Command for verifying authentication
                if re.match(reg_api_auth, raw_cmd):
                    pair = re.search(reg_api_auth, raw_cmd)
                    uapi = pair.group(1)

                    if self.clientNeedsVerify(rec_from_uid):
                        LOG.info("Received verify response from %s", rec_from_name)
                        auth = TS3Auth.AuthRequest(uapi)

                        LOG.debug('Name: |%s| API: |%s|' % (auth.name, uapi))

                        if auth.success:
                            limit_hit = self.TsClientLimitReached(auth.name)
                            if Config.DEBUG:
                                LOG.debug("Limit hit check: %s", limit_hit)
                            if not limit_hit:
                                LOG.info("Setting permissions for %s as verified.", rec_from_name)

                                # set permissions
                                self.setPermissions(rec_from_uid)

                                # get todays date
                                today_date = datetime.date.today()

                                # Add user to database so we can query their API key over time to ensure they are still on our server
                                self.addUserToDB(rec_from_uid, auth.name, uapi, today_date, today_date)
                                self.updateGuildTags(User(self.ts_connection, unique_id=rec_from_uid, ex_hand=signal_exception_handler), auth)
                                # self.updateGuildTags(rec_from_uid, auth)
                                LOG.debug("Added user to DB with ID %s", rec_from_uid)

                                # notify user they are verified
                                self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_msg_success"))
                            else:
                                # client limit is set and hit
                                self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_msg_limit_Hit"))
                                LOG.info("Received API Auth from %s, but %s has reached the client limit.", rec_from_name, rec_from_name)
                        else:
                            # Auth Failed
                            self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_msg_fail"))
                    else:
                        LOG.debug("Received API Auth from %s, but %s is already verified. Notified user as such.", rec_from_name, rec_from_name)
                        self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_msg_alrdy_verified"))
                else:
                    self._ts_repository.send_text_message_to_client(rec_from_id, Config.locale.get("bot_msg_rcv_default"))
                    LOG.info("Received bad response from %s [msg= %s]", rec_from_name, raw_cmd.encode('utf-8'))
                    # sys.exit(0)
        except Exception as e:
            LOG.error("BOT Event: Something went wrong during message received from teamspeak server. Likely bad user command/message.")
            LOG.error(e)
            LOG.error(traceback.format_exc())
        return None


#######################################

class Ticker(object):
    """
    Class that schedules events regularly and wraps the TS3Bot.
    """

    def __init__(self, ts3bot, interval):
        self.ts3bot = ts3bot
        self.interval = interval
        schedule.every(interval).seconds.do(self.execute)

    def execute(self):
        pass


#######################################

class Channel(object):
    def __init__(self, ts_conn, channel_id):
        self.ts_conn = ts_conn
        self.channel_id = channel_id


#######################################

class User(object):
    """
    Class that interfaces the Teamspeak-API with user-specific calls more convenient.
    Since calls to the API are penalised, the class also tries to minimise those calls
    by only resolving properties when they are actually needed and then caching them (if sensible).
    """

    def __init__(self, ts_conn, unique_id=None, ts_db_id=None, client_id=None, ex_hand=None):
        self.ts_conn = ts_conn
        self._unique_id = unique_id
        self._ts_db_id = ts_db_id
        self._client_id = client_id
        self._exception_handler = ex_hand if ex_hand is not None else default_exception_handler

        if all(x is None for x in [unique_id, ts_db_id, client_id]):
            raise ValueError("At least one ID must be non-null")

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "User[unique_id: %s, ts_db_id: %s, client_id: %s]" % (self.unique_id, self.ts_db_id, self._client_id)

    @property
    def current_channel(self):
        entry = next((c for c in self.ts_conn.ts3exec(lambda t: t.query("clientlist").all(), self._exception_handler)[0] if c.get("clid") == self.client_id), None)
        if entry:
            self._ts_db_id = entry.get("client_database_id")  # since we are already retrieving this information...
        return Channel(self.ts_conn, entry.get("cid")) if entry else None

    @property
    def name(self):
        return self.ts_conn.ts3exec(lambda t: t.query("clientgetids", cluid=self.unique_id).first().get("name"), self._exception_handler)[0]

    @property
    def unique_id(self):
        if self._unique_id is None:
            if self._ts_db_id is not None:
                self._unique_id, ex = self.ts_conn.ts3exec(lambda t: t.query("clientgetnamefromdbid", cldbid=self._ts_db_id).first().get("cluid"), self._exception_handler)
            elif self._client_id is not None:
                ids, ex = self.ts_conn.ts3exec(lambda t: t.query("clientinfo", clid=self._client_id).first(), self._exception_handler)
                self._unique_id = ids.get("client_unique_identifier")
                self._ts_db_id = ids.get("client_databased_id")  # not required, but since we already queried it...
            else:
                raise ValueError("Unique ID can not be retrieved")
        return self._unique_id

    @property
    def ts_db_id(self):
        if self._ts_db_id is None:
            if self._unique_id is not None:
                self._ts_db_id, ex = self.ts_conn.ts3exec(lambda t: t.query("clientgetdbidfromuid", cluid=self._unique_id).first().get("cldbid"), self._exception_handler)
            elif self._client_id is not None:
                ids, ex = self.ts_conn.ts3exec(lambda t: t.query("clientinfo", clid=self._client_id).first(), self._exception_handler)
                self._unique_id = ids.get("client_unique_identifier")  # not required, but since we already queried it...
                self._ts_db_id = ids.get("client_database_id")
            else:
                raise ValueError("TS DB ID can not be retrieved")
        return self._ts_db_id

    @property
    def client_id(self):
        if self._client_id is None:
            if self._unique_id is not None:
                # easiest case: unique ID is set
                self._client_id, ex = self.ts_conn.ts3exec(lambda t: t.query("clientgetids", cluid=self._unique_id).first().get("clid"), self._exception_handler)
            elif self._ts_db_id is not None:
                self._unique_id, ex = self.ts_conn.ts3exec(lambda t: t.query("clientgetnamefromdbid", cldbid=self._ts_db_id).first().get("cluid"), self._exception_handler)
                self._client_id, ex = self.ts_conn.ts3exec(lambda t: t.query("clientgetids", cluid=self._unique_id).first().get("clid"), self._exception_handler)
            else:
                raise ValueError("Client ID can not be retrieved")
        return self._client_id

    #######################################


class CommanderChecker(object):
    def __init__(self, ts3bot, commander_group_names):
        self.commander_group_names = commander_group_names
        self.ts3bot = ts3bot

        cgroups = list(filter(lambda g: g.get("name") in commander_group_names, self.ts3bot.ts_connection.ts3exec(lambda t: t.query("channelgrouplist").all())[0]))
        if len(cgroups) < 1:
            LOG.info("Could not find any group of %s to determine commanders by. Disabling this feature.", str(commander_group_names))
            self.commander_groups = []
            return

        self.commander_groups = [c.get("cgid") for c in cgroups]

    def execute(self):
        if not self.commander_groups:
            return  # disabled if no groups were found

        active_commanders = []

        def retrieve_commanders(tsc):
            command = tsc.query("channelgroupclientlist")
            for cgid in self.commander_groups:
                command.pipe(cgid=cgid)
            return command.all()

        acs, ts3qe = self.ts3bot.ts_connection.ts3exec(retrieve_commanders, signal_exception_handler)
        if ts3qe:  # check for .resp, could be another exception type
            if ts3qe.resp is not None:
                if ts3qe.resp.error["id"] != "1281":
                    # 1281 is "database empty result set", which is an expected error
                    # if not a single user currently wears a tag.
                    LOG.error("Error while trying to resolve active commanders: %s.", str(ts3qe))
            else:
                LOG.error(ts3qe)
        else:
            active_commanders_entries = [(c, self.ts3bot.getUserDBEntry(self.ts3bot.getTsUniqueID(c.get("cldbid")))) for c in acs]
            for ts_entry, db_entry in active_commanders_entries:
                if db_entry is not None:  # or else the user with the commander group was not registered and therefore not in the DB
                    u = User(self.ts3bot.ts_connection, ts_db_id=ts_entry.get("cldbid"))
                    if u.current_channel.channel_id == ts_entry.get("cid"):
                        # user could have the group in a channel but not be in there atm
                        ac = {}
                        ac["account_name"] = db_entry["account_name"]
                        ac["ts_cluid"] = db_entry["ts_db_id"]
                        ac["ts_display_name"], ex1 = self.ts3bot.ts_connection.ts3exec(
                            lambda t: t.query("clientgetnamefromuid", cluid=db_entry["ts_db_id"]).first().get("name"))  # no, there is probably no easier way to do this. I checked.
                        ac["ts_channel_name"], ex2 = self.ts3bot.ts_connection.ts3exec(lambda t: t.query("channelinfo", cid=ts_entry.get("cid")).first().get("channel_name"))
                        if ex1 or ex2:
                            LOG.warning("Could not determine information for commanding user with ID %s: '%s'. Skipping." % (str(ts_entry), ", ".join([str(e) for e in [ex1, ex2] if e is not None])))
                        else:
                            active_commanders.append(ac)
        return {"commanders": active_commanders}

#######################################
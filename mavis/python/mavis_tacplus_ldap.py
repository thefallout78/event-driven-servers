#!/usr/bin/python3
# $Id$
#
# mavis_tacplus_ldap.py
# (C)2023 Marc Huber <Marc.Huber@web.de>
# All rights reserved.
#
# TACACS+ NG backend for libmavis_external.so
# Authenticates/authorizes against LDAP, optionally supports password changes.
# Supported servers are AD and OpenLDAP, the latter with memberOf overlay only.
# Password changing for OpenLDAP requires a multi-master configuration.
#

"""
Test input for authentication:
0 TACPLUS
4 $USER
8 $PASS
49 AUTH
=

printf "0 TACPLUS\n4 $USER\n8 $PASS\n49 AUTH\n=\n" | this_script.py

Test input for password change:
0 TACPLUS
4 $USER
8 $PASS
49 CHPW
50 $NEWPASS
=

printf "0 TACPLUS\n4 $USER\n8 $PASS\n49 CHPW\n50 $NEWPASS\n=\n" | \
 this_script.py

#######

Environment variables:

LDAP_HOST
	Space-separated list of LDAP URLs or IP addresses or hostnames
	Examples: "ldap01 ldap02", "ldaps://ads01:636 ldaps://ads02:636"

LDAP_SCOPE
	LDAP search scope (BASE, LEVEL, SUBTREE)
	Default: SUBTREE

LDAP_BASE
	Base DN of your LDAP server
	Example: "dc=example,dc=com"

LDAP_CONNECT_TIMEOUT
	Timeout for initital connect to remote LDAP server. Default: 5

LDAP_USER
	User DN to use for LDAP bind if server doesn't permit anonymous searches.
	Default: unset

LDAP_PASSWD
	Password for LDAP_USER
	Default: unset

TLS_OPTIONS
	Extra options for LDAPS or STARTTLS, in Python hash syntax.
	See https://ldap3.readthedocs.io/en/latest/ssltls.html for details.
	Default: None
	Example: "version=ssl.PROTOCOL_TLSv1_3"

"""

import os, sys, re, ldap3
from mavis import ( Mavis,
	MAVIS_DOWN, MAVIS_FINAL,
	AV_V_RESULT_OK, AV_V_RESULT_ERROR, AV_V_RESULT_FAIL,
	AV_V_RESULT_NOTFOUND
)

# A helper function to evaluate environment variables: #######################
def eval_env(var, dflt):
	globals()[var] = os.getenv(var) or dflt
	return globals()[var]

# Environment variable evaluation ############################################
tls = None
if eval_env('TLS_OPTIONS', None) is not None:
	tls = eval("{ " + TLS_OPTIONS + "}")
eval_env('LDAP_HOSTS', 'ldaps://localhost')
server_pool = ldap3.ServerPool(None, ldap3.ROUND_ROBIN, active=True)
for server in LDAP_HOSTS.split():
	server_object = ldap3.Server(server, get_info=ldap3.DSA, tls=tls)
	server_pool.add(server_object)
eval_env('LDAP_BASE', 'dc=example,dc=local')
eval_env('LDAP_USER', None)
eval_env('LDAP_PASSWD', None)
eval_env('LDAP_SCOPE', 'SUBTREE')
eval_env('LDAP_CONNECT_TIMEOUT', 5)
memberof_regex = re.compile(eval_env('MEMBEROF_REGEX', '(?i)^cn=([^,]+),.*'))

# Default to OpenLDAP: #######################################################
conn = ldap3.Connection(server_pool, user=LDAP_USER, password=LDAP_PASSWD,
	receive_timeout=LDAP_CONNECT_TIMEOUT, auto_bind=True)
LDAP_SERVER_TYPE="generic"
LDAP_FILTER = '(&(objectclass=posixaccount)(uid={}))'

# Check for MS AD LDAP (but only for non-anonymous binds): ####################
if LDAP_USER is not None:
	if conn.bind():
		if '1.2.840.113556.1.4.800' in map(
			lambda x: x[0], conn.server.info.supported_features):
			LDAP_SERVER_TYPE = "microsoft"
			LDAP_FILTER = '(&(objectclass=user)(sAMAccountName={}))'
			LDAP_USER = conn.user

# A helper function for resolving nested groups: #############################
def expand_memberof(g):
	H = { }
	def expand_memberof_sub(g):
		for m in g:
			if memberof_regex.match(m) and not m.memberOf in H:
				H[m.memberOf] = True
				conn.search(search_base = g, search_filter = '&((objectclass=group)',
					search_scope=BASE, attributes = ['memberOf'])
				expand_memberof_sub(conn.entries)
	for m in g:
		if memberof_regex.match(m):
			expand_memberof_sub(m)
			H[m] = True
	return H.keys()

# The main loop: #############################################################
while True:
	D = Mavis()

	if not D.is_tacplus():
		D.write(MAVIS_DOWN, None, None)
		continue
	if not D.valid():
		D.write(MAVIS_FINAL, AV_V_RESULT_ERROR, "Invalid input.")
		continue

	if conn == None:
		conn = ldap3.Connection(server_pool,
			user=LDAP_USER, password=LDAP_PASSWD,
			receive_timeout=LDAP_CONNECT_TIMEOUT, auto_bind=True)
		# Try to uise STARTTLS. Might not be required here.
		if not conn.tls_started and '1.3.6.1.4.1.1466.20037' in map (
			lambda x: x[0], conn.server.info.supported_extensions):
			conn.start_tls()
		if conn.bind():
			LDAP_USER = conn.user

	if not conn.bind():
		conn.rebind(user=LDAP_USER, password=LDAP_PASSWD)

	conn.search(search_base=LDAP_BASE, search_scope=LDAP_SCOPE,
		search_filter=LDAP_FILTER.format(D.user),
		attributes=["memberOf", "shadowExpire", "uidNumber", "gidNumber",
			"loginShell", "homeDirectory"])
	if len(conn.entries) == 0:
		D.write(MAVIS_FINAL, AV_V_RESULT_NOTFOUND, None)
	elif len(conn.entries) != 1:
		D.write(MAVIS_FINAL, AV_V_RESULT_ERROR, "User name not unique.")
		continue

	entry = conn.entries[0]

	def auth_failed(D, conn):
		message = conn.result["description"]
		if conn.result["message"] != "":
			message += " (" + conn.result["message"] + ")"
		D.write(MAVIS_FINAL, AV_V_RESULT_FAIL,
			"Permission denied: " + message + ".")

	if D.is_tacplus_authc:
		if (LDAP_SERVER_TYPE == "generic"
			and len(entry.shadowExpire) > 0  and int(entry.shadowExpire[0]) > 0
			and int(entry.shadowExpire[0]) * 86400 < time.time()):
			D.write(MAVIS_FINAL, AV_V_RESULT_FAIL,
				"Password has expired.")
			continue;
		if not conn.rebind(user=entry.entry_dn, password=D.password):
			auth_failed(D, conn)
			continue
		D.set_dbpassword(D.password)

	user_msg = None
	if D.is_tacplus_chpw:
		if ((LDAP_SERVER_TYPE == "microsoft"
				and not conn.extend.microsoft.modify_password(
				entry.entry_dn, D.password, D.password_new))
			or  (LDAP_SERVER_TYPE == "generic"
				and not conn.extend.standard.modify_password (
				entry.entry_dn, D.password, D.password_new))):
			auth_failed(D, conn)
			continue;
		user_msg = "Password change was successful."
		D.set_dbpassword(D.password_new)

	D.set_dn(entry.entry_dn)
	if len(entry.uidNumber) > 0:
		D.set_uid(entry.uidNumber[0])
	if len(entry.gidNumber) > 0:
		D.set_gid(entry.uidNumber[0])
	if len(entry.loginShell) > 0:
		D.set_shell(entry.loginShell[0])
	if len(entry.homeDirectory) > 0:
		D.set_home(entry.homeDirectory[0])

	if entry.memberOf != "":
		L = expand_memberof(entry.memberOf)
		D.set_memberof("\"" + "\",\"".join(L) + "\"")
		L = [memberof_regex.sub(r'\1', l) for l in L]
		D.set_tacmember("\"" + "\",\"".join(L) + "\"")

	D.write(MAVIS_FINAL, AV_V_RESULT_OK, user_msg);

# End
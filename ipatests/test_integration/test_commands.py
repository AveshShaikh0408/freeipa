#
# Copyright (C) 2018  FreeIPA Contributors see COPYING for license
#
"""Misc test for 'ipa' CLI regressions
"""
from __future__ import absolute_import

import base64
import re
import os
import logging
import ssl
from tempfile import NamedTemporaryFile
from itertools import chain, repeat
import textwrap
import time
import paramiko
import pytest

from ipaplatform.paths import paths

from ipatests.test_integration.base import IntegrationTest
from ipatests.pytest_ipa.integration import tasks
from ipatests.create_external_ca import ExternalCA

logger = logging.getLogger(__name__)

class TestIPACommand(IntegrationTest):
    """
    A lot of commands can be executed against a single IPA installation
    so provide a generic class to execute one-off commands that need to be
    tested without having to fire up a full server to run one command.
    """
    topology = 'line'

    def get_cert_base64(self, host, path):
        """Retrieve cert and return content as single line, base64 encoded
        """
        cacrt = host.get_file_contents(path, encoding='ascii')
        cader = ssl.PEM_cert_to_DER_cert(cacrt)
        return base64.b64encode(cader).decode('ascii')

    def test_certmap_match_issue7520(self):
        # https://pagure.io/freeipa/issue/7520
        tasks.kinit_admin(self.master)
        result = self.master.run_command(
            ['ipa', 'certmap-match', paths.IPA_CA_CRT],
            raiseonerr=False
        )
        assert result.returncode == 1
        assert not result.stderr_text
        assert "0 users matched" in result.stdout_text

        cab64 = self.get_cert_base64(self.master, paths.IPA_CA_CRT)
        result = self.master.run_command(
            ['ipa', 'certmap-match', '--certificate', cab64],
            raiseonerr=False
        )
        assert result.returncode == 1
        assert not result.stderr_text
        assert "0 users matched" in result.stdout_text

    def test_cert_find_issue7520(self):
        # https://pagure.io/freeipa/issue/7520
        tasks.kinit_admin(self.master)
        subject = 'CN=Certificate Authority,O={}'.format(
            self.master.domain.realm)

        # by cert file
        result = self.master.run_command(
            ['ipa', 'cert-find', '--file', paths.IPA_CA_CRT]
        )
        assert subject in result.stdout_text
        assert '1 certificate matched' in result.stdout_text

        # by base64 cert
        cab64 = self.get_cert_base64(self.master, paths.IPA_CA_CRT)
        result = self.master.run_command(
            ['ipa', 'cert-find', '--certificate', cab64]
        )
        assert subject in result.stdout_text
        assert '1 certificate matched' in result.stdout_text

    def test_add_permission_failure_issue5923(self):
        # https://pagure.io/freeipa/issue/5923
        # error response used to contain bytes instead of text

        tasks.kinit_admin(self.master)
        # neither privilege nor permission exists
        result = self.master.run_command(
            ["ipa", "privilege-add-permission", "loc",
             "--permission='System: Show IPA Locations"],
            raiseonerr=False
        )
        assert result.returncode == 2
        err = result.stderr_text.strip()  # pylint: disable=no-member
        assert err == "ipa: ERROR: loc: privilege not found"
        # add privilege
        result = self.master.run_command(
            ["ipa", "privilege-add", "loc"],
        )
        assert 'Added privilege "loc"' in result.stdout_text
        # permission is still missing
        result = self.master.run_command(
            ["ipa", "privilege-add-permission", "loc",
             "--permission='System: Show IPA Locations"],
            raiseonerr=False
        )
        assert result.returncode == 1
        assert "Number of permissions added 0" in result.stdout_text

    def test_change_sysaccount_password_issue7561(self):
        sysuser = 'system'
        original_passwd = 'Secret123'
        new_passwd = 'userPasswd123'

        master = self.master

        base_dn = str(master.domain.basedn)  # pylint: disable=no-member
        tf = NamedTemporaryFile()
        ldif_file = tf.name
        entry_ldif = textwrap.dedent("""
            dn: uid=system,cn=sysaccounts,cn=etc,{base_dn}
            changetype: add
            objectclass: account
            objectclass: simplesecurityobject
            uid: system
            userPassword: {original_passwd}
            passwordExpirationTime: 20380119031407Z
            nsIdleTimeout: 0
        """).format(
            base_dn=base_dn,
            original_passwd=original_passwd)
        master.put_file_contents(ldif_file, entry_ldif)
        arg = ['ldapmodify',
               '-h', master.hostname,
               '-p', '389', '-D',
               str(master.config.dirman_dn),   # pylint: disable=no-member
               '-w', master.config.dirman_password,
               '-f', ldif_file]
        master.run_command(arg)

        tasks.ldappasswd_sysaccount_change(sysuser, original_passwd,
                                           new_passwd, master)

    def test_ldapmodify_password_issue7601(self):
        user = 'ipauser'
        original_passwd = 'Secret123'
        new_passwd = 'userPasswd123'
        new_passwd2 = 'mynewPwd123'
        master = self.master
        base_dn = str(master.domain.basedn)  # pylint: disable=no-member

        # Create a user with a password
        tasks.kinit_admin(master)
        add_password_stdin_text = "{pwd}\n{pwd}".format(pwd=original_passwd)
        master.run_command(['ipa', 'user-add', user,
                            '--first', user,
                            '--last', user,
                            '--password'],
                           stdin_text=add_password_stdin_text)
        # kinit as that user in order to modify the pwd
        user_kinit_stdin_text = "{old}\n%{new}\n%{new}\n".format(
            old=original_passwd,
            new=original_passwd)
        master.run_command(['kinit', user], stdin_text=user_kinit_stdin_text)
        # Retrieve krblastpwdchange and krbpasswordexpiration
        search_cmd = [
            'ldapsearch', '-x',
            '-D', 'cn=directory manager',
            '-w', master.config.dirman_password,
            '-s', 'base',
            '-b', 'uid={user},cn=users,cn=accounts,{base_dn}'.format(
                user=user, base_dn=base_dn),
            '-o', 'ldif-wrap=no',
            '-LLL',
            'krblastpwdchange',
            'krbpasswordexpiration']
        output = master.run_command(search_cmd).stdout_text.lower()

        # extract krblastpwdchange and krbpasswordexpiration
        krbchg_pattern = 'krblastpwdchange: (.+)\n'
        krbexp_pattern = 'krbpasswordexpiration: (.+)\n'
        krblastpwdchange = re.findall(krbchg_pattern, output)[0]
        krbexp = re.findall(krbexp_pattern, output)[0]

        # sleep 1 sec (krblastpwdchange and krbpasswordexpiration have at most
        # a 1s precision)
        time.sleep(1)
        # perform ldapmodify on userpassword as dir mgr
        mod = NamedTemporaryFile()
        ldif_file = mod.name
        entry_ldif = textwrap.dedent("""
            dn: uid={user},cn=users,cn=accounts,{base_dn}
            changetype: modify
            replace: userpassword
            userpassword: {new_passwd}
        """).format(
            user=user,
            base_dn=base_dn,
            new_passwd=new_passwd)
        master.put_file_contents(ldif_file, entry_ldif)
        arg = ['ldapmodify',
               '-h', master.hostname,
               '-p', '389', '-D',
               str(master.config.dirman_dn),   # pylint: disable=no-member
               '-w', master.config.dirman_password,
               '-f', ldif_file]
        master.run_command(arg)

        # Test new password with kinit
        master.run_command(['kinit', user], stdin_text=new_passwd)
        # Retrieve krblastpwdchange and krbpasswordexpiration
        output = master.run_command(search_cmd).stdout_text.lower()
        # extract krblastpwdchange and krbpasswordexpiration
        newkrblastpwdchange = re.findall(krbchg_pattern, output)[0]
        newkrbexp = re.findall(krbexp_pattern, output)[0]

        # both should have changed
        assert newkrblastpwdchange != krblastpwdchange
        assert newkrbexp != krbexp

        # Now test passwd modif with ldappasswd
        time.sleep(1)
        master.run_command([
            paths.LDAPPASSWD,
            '-D', str(master.config.dirman_dn),   # pylint: disable=no-member
            '-w', master.config.dirman_password,
            '-a', new_passwd,
            '-s', new_passwd2,
            '-x', '-ZZ',
            '-H', 'ldap://{hostname}'.format(hostname=master.hostname),
            'uid={user},cn=users,cn=accounts,{base_dn}'.format(
                user=user, base_dn=base_dn)]
        )
        # Test new password with kinit
        master.run_command(['kinit', user], stdin_text=new_passwd2)
        # Retrieve krblastpwdchange and krbpasswordexpiration
        output = master.run_command(search_cmd).stdout_text.lower()
        # extract krblastpwdchange and krbpasswordexpiration
        newkrblastpwdchange2 = re.findall(krbchg_pattern, output)[0]
        newkrbexp2 = re.findall(krbexp_pattern, output)[0]

        # both should have changed
        assert newkrblastpwdchange != newkrblastpwdchange2
        assert newkrbexp != newkrbexp2

    def test_change_selinuxusermaporder(self):
        """
        An update file meant to ensure a more sane default was
        overriding any customization done to the order.
        """
        maporder = "unconfined_u:s0-s0:c0.c1023"

        # set a new default
        tasks.kinit_admin(self.master)
        result = self.master.run_command(
            ["ipa", "config-mod",
             "--ipaselinuxusermaporder={}".format(maporder)],
            raiseonerr=False
        )
        assert result.returncode == 0

        # apply the update
        result = self.master.run_command(
            ["ipa-server-upgrade"],
            raiseonerr=False
        )
        assert result.returncode == 0

        # ensure result is the same
        result = self.master.run_command(
            ["ipa", "config-show"],
            raiseonerr=False
        )
        assert result.returncode == 0
        assert "SELinux user map order: {}".format(
            maporder) in result.stdout_text

    def test_ipa_console(self):
        tasks.kinit_admin(self.master)
        result = self.master.run_command(
            ["ipa", "console"],
            stdin_text="api.env"
        )
        assert "ipalib.config.Env" in result.stdout_text

        filename = tasks.upload_temp_contents(
            self.master,
            "print(api.env)\n"
        )
        result = self.master.run_command(
            ["ipa", "console", filename],
        )
        assert "ipalib.config.Env" in result.stdout_text

    def test_list_help_topics(self):
        tasks.kinit_admin(self.master)
        result = self.master.run_command(
            ["ipa", "help", "topics"],
            raiseonerr=False
        )
        assert result.returncode == 0

    def test_ssh_key_connection(self, tmpdir):
        """
        Integration test for https://pagure.io/SSSD/sssd/issue/3747
        """

        test_user = 'test-ssh'
        master = self.master.hostname

        pub_keys = []

        for i in range(40):
            ssh_key_pair = tasks.generate_ssh_keypair()
            pub_keys.append(ssh_key_pair[1])
            with open(os.path.join(
                    tmpdir, 'ssh_priv_{}'.format(i)), 'w') as fp:
                fp.write(ssh_key_pair[0])

        tasks.kinit_admin(self.master)
        self.master.run_command(['ipa', 'user-add', test_user,
                                 '--first=tester', '--last=tester'])

        keys_opts = ' '.join(['--ssh "{}"'.format(k) for k in pub_keys])
        cmd = 'ipa user-mod {} {}'.format(test_user, keys_opts)
        self.master.run_command(cmd)

        # connect with first SSH key
        first_priv_key_path = os.path.join(tmpdir, 'ssh_priv_1')
        # change private key permission to comply with SS rules
        os.chmod(first_priv_key_path, 0o600)

        sshcon = paramiko.SSHClient()
        sshcon.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # first connection attempt is a workaround for
        # https://pagure.io/SSSD/sssd/issue/3669
        try:
            sshcon.connect(master, username=test_user,
                           key_filename=first_priv_key_path, timeout=1)
        except (paramiko.AuthenticationException, paramiko.SSHException):
            pass

        try:
            sshcon.connect(master, username=test_user,
                           key_filename=first_priv_key_path, timeout=1)
        except (paramiko.AuthenticationException,
                paramiko.SSHException) as e:
            pytest.fail('Authentication using SSH key not successful', e)

        journal_cmd = ['journalctl', '--since=today', '-u', 'sshd']
        result = self.master.run_command(journal_cmd)
        output = result.stdout_text
        assert not re.search('exited on signal 13', output)

        # cleanup
        self.master.run_command(['ipa', 'user-del', test_user])

    def test_ssh_leak(self):
        """
        Integration test for https://pagure.io/SSSD/sssd/issue/3794
        """

        def count_pipes():

            res = self.master.run_command(['pidof', 'sssd_ssh'])
            pid = res.stdout_text.strip()
            proc_path = '/proc/{}/fd'.format(pid)
            res = self.master.run_command(['ls', '-la', proc_path])
            fds_text = res.stdout_text.strip()
            return sum((1 for _ in re.finditer(r'pipe', fds_text)))

        test_user = 'test-ssh'

        tasks.kinit_admin(self.master)
        self.master.run_command(['ipa', 'user-add', test_user,
                                 '--first=tester', '--last=tester'])

        certs = []

        # we are ok with whatever certificate for this test
        external_ca = ExternalCA()
        for _dummy in range(3):
            cert = external_ca.create_ca()
            cert = tasks.strip_cert_header(cert.decode('utf-8'))
            certs.append('"{}"'.format(cert))

        cert_args = list(
            chain.from_iterable(list(zip(repeat('--certificate'), certs))))
        cmd = 'ipa user-add-cert {} {}'.format(test_user, ' '.join(cert_args))
        self.master.run_command(cmd)

        tasks.clear_sssd_cache(self.master)

        num_of_pipes = count_pipes()

        for _dummy in range(3):
            self.master.run_command([paths.SSS_SSH_AUTHORIZEDKEYS, test_user])
            current_num_of_pipes = count_pipes()
            assert current_num_of_pipes == num_of_pipes

        # cleanup
        self.master.run_command(['ipa', 'user-del', test_user])

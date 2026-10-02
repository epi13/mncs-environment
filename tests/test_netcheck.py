"""Reachability probe separates DNS, TLS, HTTP, and git failures."""

from __future__ import annotations

import socket
import ssl
import subprocess
import unittest
from unittest import mock

from mncs_env import netcheck as netcheck_module


class ClassifyTests(unittest.TestCase):
    def test_dns_markers(self):
        self.assertEqual(
            netcheck_module.classify_git_stderr(
                "fatal: unable to access 'https://github.com/x/y.git/': "
                "Could not resolve host: github.com"),
            "dns-resolution-failed")

    def test_connect_markers(self):
        self.assertEqual(
            netcheck_module.classify_git_stderr("fatal: Failed to connect to github.com"),
            "tcp-connect-failed")

    def test_auth_markers(self):
        self.assertEqual(
            netcheck_module.classify_git_stderr("remote: Invalid username or password.\nfatal: Authentication failed"),
            "authentication-failed")
        self.assertEqual(netcheck_module.classify_git_stderr("error: 401 Unauthorized"),
                         "authentication-failed")

    def test_not_found_markers(self):
        self.assertEqual(
            netcheck_module.classify_git_stderr("remote: Repository not found.\nfatal: repository not found"),
            "repository-not-found")

    def test_unknown_stderr_stays_generic(self):
        self.assertEqual(netcheck_module.classify_git_stderr("fatal: something novel"),
                         "git-command-failed")


class LayerTests(unittest.TestCase):
    def test_dns_failure_short_circuits(self):
        with mock.patch.object(netcheck_module.socket, "getaddrinfo",
                               side_effect=socket.gaierror(-2, "Name or service not known")):
            report = netcheck_module.check("github.com", remote="https://github.com/x/y.git")
        self.assertEqual(report["dns"]["status"], "unavailable")
        self.assertEqual(report["dns"]["reason"], "dns-resolution-failed")
        self.assertEqual(report["tcp_tls"]["status"], "skipped")
        self.assertEqual(report["git"]["status"], "skipped")
        self.assertEqual(report["verdict"], "dns-resolution-failed")

    def test_tls_failure_reports_tls_layer(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
        with mock.patch.object(netcheck_module.socket, "getaddrinfo", return_value=infos), \
                mock.patch.object(netcheck_module.socket, "create_connection",
                                  side_effect=ConnectionRefusedError("refused")):
            report = netcheck_module.check("github.com")
        self.assertEqual(report["dns"]["status"], "ok")
        self.assertEqual(report["tcp_tls"]["reason"], "tcp-connect-failed")
        self.assertEqual(report["https"]["status"], "skipped")
        self.assertEqual(report["verdict"], "tcp-connect-failed")

    def test_https_server_error_narrows_verdict(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
        response = mock.Mock()
        response.status = 503
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(netcheck_module.socket, "getaddrinfo", return_value=infos), \
                mock.patch.object(netcheck_module.socket, "create_connection"), \
                mock.patch.object(netcheck_module.ssl, "create_default_context") as context_factory, \
                mock.patch.object(netcheck_module.http.client, "HTTPSConnection",
                                  return_value=connection):
            context_factory.return_value.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = {}
            report = netcheck_module.check("github.com")
        self.assertEqual(report["https"]["reason"], "https-server-error")
        self.assertEqual(report["verdict"], "https-server-error")

    def test_git_auth_failure_classified(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
        response = mock.Mock()
        response.status = 200
        connection = mock.Mock()
        connection.getresponse.return_value = response
        failed = subprocess.CompletedProcess(
            ["git", "ls-remote"], 128, "", "fatal: Authentication failed\n")
        with mock.patch.object(netcheck_module.socket, "getaddrinfo", return_value=infos), \
                mock.patch.object(netcheck_module.socket, "create_connection"), \
                mock.patch.object(netcheck_module.ssl, "create_default_context") as context_factory, \
                mock.patch.object(netcheck_module.http.client, "HTTPSConnection",
                                  return_value=connection), \
                mock.patch.object(netcheck_module.subprocess, "run", return_value=failed):
            context_factory.return_value.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = {}
            report = netcheck_module.check(
                "github.com", remote="https://github.com/x/private.git")
        self.assertEqual(report["git"]["reason"], "authentication-failed")
        self.assertEqual(report["verdict"], "authentication-failed")

    def test_full_success_reports_reachable(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
        response = mock.Mock()
        response.status = 200
        connection = mock.Mock()
        connection.getresponse.return_value = response
        ok = subprocess.CompletedProcess(
            ["git", "ls-remote"], 0, "abc123\tHEAD\n", "")
        with mock.patch.object(netcheck_module.socket, "getaddrinfo", return_value=infos), \
                mock.patch.object(netcheck_module.socket, "create_connection"), \
                mock.patch.object(netcheck_module.ssl, "create_default_context") as context_factory, \
                mock.patch.object(netcheck_module.http.client, "HTTPSConnection",
                                  return_value=connection), \
                mock.patch.object(netcheck_module.subprocess, "run", return_value=ok):
            context_factory.return_value.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = {}
            report = netcheck_module.check(
                "github.com", remote="https://github.com/x/y.git")
        self.assertEqual(report["verdict"], "reachable")
        self.assertEqual(report["git"]["tip"], "abc123")


if __name__ == "__main__":
    unittest.main()

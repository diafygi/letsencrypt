"""Renewer tool.

Renewer tool handles autorenewal and autodeployment of renewed certs
within lineages of successor certificates, according to configuration.

.. todo:: Sanity checking consistency, validity, freshness?
.. todo:: Call new installer API to restart servers after deployment

"""
import argparse
import os
import sys

import configobj
import zope.component

from letsencrypt import configuration
from letsencrypt import cli
from letsencrypt import client
from letsencrypt import crypto_util
from letsencrypt import notify
from letsencrypt import storage

from letsencrypt.display import util as display_util
from letsencrypt.plugins import disco as plugins_disco


class _AttrDict(dict):
    """Attribute dictionary.

    A trick to allow accessing dictionary keys as object attributes.

    """
    def __init__(self, *args, **kwargs):
        super(_AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self


def renew(cert, old_version):
    """Perform automated renewal of the referenced cert, if possible.

    :param letsencrypt.storage.RenewableCert cert: The certificate
        lineage to attempt to renew.
    :param int old_version: The version of the certificate lineage
        relative to which the renewal should be attempted.

    :returns: A number referring to newly created version of this cert
        lineage, or ``False`` if renewal was not successful.
    :rtype: `int` or `bool`

    """
    # TODO: handle partial success (some names can be renewed but not
    #       others)
    # TODO: handle obligatory key rotation vs. optional key rotation vs.
    #       requested key rotation
    if "renewalparams" not in cert.configfile:
        # TODO: notify user?
        return False
    renewalparams = cert.configfile["renewalparams"]
    if "authenticator" not in renewalparams:
        # TODO: notify user?
        return False
    # Instantiate the appropriate authenticator
    plugins = plugins_disco.PluginsRegistry.find_all()
    config = configuration.NamespaceConfig(_AttrDict(renewalparams))
    # XXX: this loses type data (for example, the fact that key_size
    #      was an int, not a str)
    config.rsa_key_size = int(config.rsa_key_size)
    config.dvsni_port = int(config.dvsni_port)
    try:
        authenticator = plugins[renewalparams["authenticator"]]
    except KeyError:
        # TODO: Notify user? (authenticator could not be found)
        return False
    authenticator = authenticator.init(config)

    authenticator.prepare()
    account = client.determine_account(config)
    # TODO: are there other ways to get the right account object, e.g.
    #       based on the email parameter that might be present in
    #       renewalparams?

    our_client = client.Client(config, account, authenticator, None)
    with open(cert.version("cert", old_version)) as f:
        sans = crypto_util.get_sans_from_cert(f.read())
    new_certr, new_chain, new_key, _ = our_client.obtain_certificate(sans)
    if new_chain is not None:
        # XXX: Assumes that there was no key change.  We need logic
        #      for figuring out whether there was or not.  Probably
        #      best is to have obtain_certificate return None for
        #      new_key if the old key is to be used (since save_successor
        #      already understands this distinction!)
        return cert.save_successor(old_version, new_certr.body.as_pem(),
                                   new_key.pem, new_chain.as_pem())
        # TODO: Notify results
    else:
        # TODO: Notify negative results
        return False
    # TODO: Consider the case where the renewal was partially successful
    #       (where fewer than all names were renewed)


def _paths_parser(parser):
    add = parser.add_argument_group("paths").add_argument
    add("--config-dir", default=cli.flag_default("config_dir"),
        help=cli.config_help("config_dir"))
    add("--work-dir", default=cli.flag_default("work_dir"),
        help=cli.config_help("work_dir"))
    add("--logs-dir", default=cli.flag_default("logs_dir"),
        help="Path to a directory where logs are stored.")

    return parser


def _create_parser():
    parser = argparse.ArgumentParser()
    #parser.add_argument("--cron", action="store_true", help="Run as cronjob.")
    # pylint: disable=protected-access
    return _paths_parser(parser)


def main(config=None, args=sys.argv[1:]):
    """Main function for autorenewer script."""
    # TODO: Distinguish automated invocation from manual invocation,
    #       perhaps by looking at sys.argv[0] and inhibiting automated
    #       invocations if /etc/letsencrypt/renewal.conf defaults have
    #       turned it off. (The boolean parameter should probably be
    #       called renewer_enabled.)

    zope.component.provideUtility(display_util.FileDisplay(sys.stdout))

    cli_config = configuration.RenewerConfiguration(
        _create_parser().parse_args(args))

    config = storage.config_with_defaults(config)
    # Now attempt to read the renewer config file and augment or replace
    # the renewer defaults with any options contained in that file.  If
    # renewer_config_file is undefined or if the file is nonexistent or
    # empty, this .merge() will have no effect.  TODO: when we have a more
    # elaborate renewer command line, we will presumably also be able to
    # specify a config file on the command line, which, if provided, should
    # take precedence over this one.
    config.merge(configobj.ConfigObj(cli_config.renewer_config_file))

    for i in os.listdir(cli_config.renewal_configs_dir):
        print "Processing", i
        if not i.endswith(".conf"):
            continue
        rc_config = configobj.ConfigObj(cli_config.renewer_config_file)
        rc_config.merge(configobj.ConfigObj(
            os.path.join(cli_config.renewal_configs_dir, i)))
        # TODO: this is a dirty hack!
        rc_config.filename = os.path.join(cli_config.renewal_configs_dir, i)
        try:
            # TODO: Before trying to initialize the RenewableCert object,
            #       we could check here whether the combination of the config
            #       and the rc_config together disables all autorenewal and
            #       autodeployment applicable to this cert.  In that case, we
            #       can simply continue and don't need to instantiate a
            #       RenewableCert object for this cert at all, which could
            #       dramatically improve performance for large deployments
            #       where autorenewal is widely turned off.
            cert = storage.RenewableCert(rc_config, cli_config=cli_config)
        except ValueError:
            # This indicates an invalid renewal configuration file, such
            # as one missing a required parameter (in the future, perhaps
            # also one that is internally inconsistent or is missing a
            # required parameter).  As a TODO, maybe we should warn the
            # user about the existence of an invalid or corrupt renewal
            # config rather than simply ignoring it.
            continue
        if cert.should_autorenew():
            # Note: not cert.current_version() because the basis for
            # the renewal is the latest version, even if it hasn't been
            # deployed yet!
            old_version = cert.latest_common_version()
            renew(cert, old_version)
            notify.notify("Autorenewed a cert!!!", "root", "It worked!")
            # TODO: explain what happened
        if cert.should_autodeploy():
            cert.update_all_links_to(cert.latest_common_version())
            # TODO: restart web server (invoke IInstaller.restart() method)
            notify.notify("Autodeployed a cert!!!", "root", "It worked!")
            # TODO: explain what happened

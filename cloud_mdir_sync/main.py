# SPDX-License-Identifier: GPL-2.0+
import argparse
import asyncio
import contextlib
import os
from typing import Dict, Optional, Tuple

import aiohttp
import pyinotify

from . import config, mailbox, messages, oauth, office365


def force_local_to_cloud(cfg: config.Config) -> messages.MBoxDict_Type:
    """Make all the local mailboxes match their cloud content, overwriting any
    local changes."""

    # For every cloud message figure out which local mailbox it belongs to
    msgs: messages.MBoxDict_Type = {}
    for mbox in cfg.local_mboxes:
        msgs[mbox] = {}
    for mbox in cfg.cloud_mboxes:
        for ch,msg in mbox.messages.items():
            dest = cfg.direct_message(msg)
            msgs[dest][ch] = msg

    for mbox, msgdict in msgs.items():
        if not mbox.same_messages(msgdict):
            mbox.force_content(cfg.msgdb, msgdict)
    return msgs


async def update_cloud_from_local(cfg: config.Config,
                                  msgs_by_local: messages.MBoxDict_Type):
    """Detect differences made by the local mailboxes and upload them to the
    cloud."""
    msgs_by_cloud: Dict[mailbox.Mailbox, messages.CHMsgMappingDict_Type] = {}
    for mbox in cfg.cloud_mboxes:
        msgs_by_cloud[mbox] = {}
    for local_mbox, msgdict in msgs_by_local.items():
        for ch, cloud_msg in msgdict.items():
            msgs_by_cloud[cloud_msg.mailbox][ch] = (
                local_mbox.messages.get(ch), cloud_msg)
    await asyncio.gather(*(
        mbox.merge_content(msgdict) for mbox, msgdict in msgs_by_cloud.items()
        if not mbox.same_messages(msgdict, tuple_form=True)))


async def synchronize_mail(cfg: config.Config):
    """Main synchronizing loop"""
    cfg.web_app = oauth.WebServer()
    try:
        await cfg.web_app.go()

        await asyncio.gather(*(mbox.setup_mbox(cfg)
                               for mbox in cfg.all_mboxes()))

        msgs = None
        while True:
            try:
                await asyncio.gather(*(mbox.update_message_list(cfg.msgdb)
                                       for mbox in cfg.all_mboxes()
                                       if mbox.need_update))

                if msgs is not None:
                    await update_cloud_from_local(cfg, msgs)

                msgs = force_local_to_cloud(cfg)
            except (FileNotFoundError, asyncio.TimeoutError,
                    aiohttp.client_exceptions.ClientError, IOError,
                    RuntimeError):
                cfg.logger.exception(
                    "Failed update cycle, sleeping then retrying")
                await asyncio.sleep(10)
                continue

            await mailbox.Mailbox.changed_event.wait()
            mailbox.Mailbox.changed_event.clear()
            cfg.msgdb.cleanup_msgs(msgs)
            cfg.logger.debug("Changed event, looping")
    finally:
        await asyncio.gather(*(domain.close()
                               for domain in cfg.domains.values()))
        cfg.domains = {}
        await cfg.web_app.close()


def main():
    parser = argparse.ArgumentParser(
        description=
        """Cloud MailDir Sync is able to download email messages from a cloud
        provider and store them in a local maildir. It uses the REST interface
        from the cloud provider rather than IMAP and uses OAUTH to
        authenticate. Once downloaded the tool tracks changes in the local
        mail dir and uploads them back to the cloud.""")
    parser.add_argument("-c",
                        dest="CFG",
                        default="cms.cfg",
                        help="Configuration file to use")
    args = parser.parse_args()

    cfg = config.Config()
    cfg.load_config(args.CFG)
    cfg.loop = asyncio.get_event_loop()
    with contextlib.closing(pyinotify.WatchManager()) as wm, \
            contextlib.closing(messages.MessageDB(cfg)) as msgdb, \
            open("trace", "wb") as trace:
        pyinotify.AsyncioNotifier(wm, cfg.loop)
        cfg.watch_manager = wm
        cfg.msgdb = msgdb
        cfg.trace_file = trace
        cfg.loop.run_until_complete(synchronize_mail(cfg))

    cfg.loop.run_until_complete(cfg.loop.shutdown_asyncgens())
    cfg.loop.close()


if __name__ == "__main__":
    main()
"""The question as the tool exits: an email address, to be emailed as more
devices are supported (the website's /v1/notify, website/api/index.js).

Asked once a sitting, however many devices it converted or read out, and
whether it finished, failed or was quit with q; not where it never began (q
at its first question, or REFUSING), was stopped with Ctrl-C or its window
closed, or nobody is at the keyboard. The sign-up names the devices in the
owner's eWeLink account, seen as the sitting went (saw_devices), and the
readouts they sent (sent_readout), so their own devices' news can reach them
too. The eWeLink account's address is offered first, taken with Enter or y.

Once an address is taken, it is kept in the settings (SETTING) and never
asked for again on this computer: each later sitting sends its devices and
readouts with it, unasked, and the website adds them to the address's if it
was confirmed. One removed or never confirmed is not emailed again; its
owner can sign up again on the website (SIGNUP_PAGE). The `email` command
(change) gives another address, or has this computer forget it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.autoflash import output, prompts, runs
from tools.ewelink.get_device_key import is_email

# Where someone can sign up instead, when the tool cannot reach the website.
SIGNUP_PAGE = "https://firmwareliberator.com/notify"
QUESTION = "Get an email as more devices are supported?"
# The settings' key for the address taken (runs.load_settings).
SETTING = "signup_email"


@dataclass
class Sitting:
    """What this sitting has seen of the owner's devices and readouts."""
    devices: dict[tuple[str, str], None] = field(default_factory=dict)   # (product, firmware model), in order
    readouts: dict[str, None] = field(default_factory=dict)   # each reference sent under
    account: str = ""    # the eWeLink account signed in to, if an email address
    stopped: bool = False   # by Ctrl-C, or its window closed: not asked


SITTING = Sitting()


def saw_devices(devices: list[dict[str, Any]]) -> None:
    """The devices eWeLink lists in the owner's account."""
    for device in devices:
        product = str(device.get("productModel") or "").strip()
        if product:
            SITTING.devices[(product, str((device.get("extra") or {}).get("model") or ""))] = None


def sent_readout(reference: str) -> None:
    """A readout the website took, by its reference (none for one it already
    had enough of)."""
    if reference:
        SITTING.readouts[reference] = None


def saw_account(name: str) -> None:
    """The eWeLink account used: offered as the address, if it is one."""
    if is_email(name):
        SITTING.account = name


def stopped() -> None:
    """The sitting was stopped with Ctrl-C, or its window closed."""
    SITTING.stopped = True


def account_address() -> str:
    """The address to offer: this sitting's eWeLink account's, or else the
    last one used on this computer, if either is an email address."""
    if SITTING.account:
        return SITTING.account
    last = str(runs.load_settings().get("ewelink_email") or "")
    return last if is_email(last) else ""


def given() -> str:
    """The address taken on this computer before, if any."""
    return str(runs.load_settings().get(SETTING) or "")


def remember(email: str | None) -> None:
    """Keeps `email` as the address given; None forgets it."""
    settings = runs.load_settings()
    if email is None:
        settings.pop(SETTING, None)
    else:
        settings[SETTING] = email
    runs.save_settings(settings)


def ask_address() -> str:
    return prompts.ask_text("Your email address", private="email",
                            validate=lambda text: (is_email(text), "That does not look like an email address."))


def change() -> int:
    """The `email` command: the address kept on this computer, changed or
    forgotten. Removing it from the list is its emails' link alone: this
    computer holds nothing that could."""
    from tools.autoflash import readout   # readout imports this module
    before = given()
    if not before:
        output.say("No email address is kept on this computer: the tool asks for one as it exits.")
        return 0
    try:
        choice = prompts.ask_choice(f"News of more devices goes to {before}.",
                                    ["Keep it", "Use a different address", "Forget it on this computer"])
        if choice == 0:
            return 0
        if choice == 2:
            remember(None)
            output.say("Forgotten on this computer. To stop the emails, use the link in any of them.")
            return 0
        email = ask_address().strip()
    except (prompts.QuitRun, KeyboardInterrupt):
        return 0
    if email.lower() == before.lower():
        output.say("That is the address kept already.")
        return 0
    if not readout.notify(email, ()):
        output.say(f"That could not be sent just now: try again later, or sign up at {SIGNUP_PAGE}.")
        return 1
    remember(email)
    output.say(f"Check your inbox to confirm. To stop emails to {before}, use the link in any of them.")
    return 0


def ask_on_exit() -> None:
    """The question, as the sitting ends: an address, and sent; or, with one
    taken before, the sitting's devices and readouts sent with it, unasked.
    Never raises: q, n or Ctrl-C leave at once."""
    from tools.autoflash import interrupt, readout   # readout imports this module
    if SITTING.stopped or prompts.UNATTENDED or interrupt.window_closed() or readout.upload_url() is None:
        return
    before = given()
    if before:
        # Only added to the address if it was confirmed: one removed by its
        # owner, or never confirmed, is not emailed again.
        if (SITTING.devices or SITTING.readouts) and not readout.notify(before, SITTING.devices, SITTING.readouts,
                                                                         update=True):
            output.log_only("    This sitting's devices were not added to the email sign-up.")
        return
    address = account_address()
    options = [*([f"Yes, at {address}", "Use a different address"] if address else ["Add an email address"]),
               "No, thank you"]
    try:
        output.tell("")
        choice = prompts.ask_choice(f"{QUESTION} [{'Y/n' if address else 'y/n'}]", options,
                                    shortcuts={**({"": 0} if address else {}), "y": 0, "yes": 0,
                                               "n": len(options) - 1, "no": len(options) - 1})
        if choice == len(options) - 1:
            return
        email = address if choice == 0 and address else ask_address()
    except (prompts.QuitRun, KeyboardInterrupt):
        return
    if readout.notify(email.strip(), SITTING.devices, SITTING.readouts):
        remember(email.strip())
        output.tell("    Check your inbox to confirm.")
    else:
        output.tell(f"    That could not be sent just now: sign up at {SIGNUP_PAGE} instead.")

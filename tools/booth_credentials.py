# /// script
# requires-python = ">=3.11"
# dependencies = ["werkzeug==3.1.3"]
# ///
"""Create a private hosted-demo env file; never overwrite an existing one."""
import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", help="Dedicated demo domain, e.g. factory.example.com")
    parser.add_argument("--output", type=Path, default=Path(".env.booth"))
    args = parser.parse_args()
    if len(args.domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain) or "." not in args.domain:
        parser.error("Use a DNS hostname, without a scheme, port or path.")
    password = getpass.getpass("Demo password (at least 12 characters): ")
    if len(password) < 12:
        parser.error("The password must contain at least 12 characters.")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords did not match.")
    content = (f"DEMO_DOMAIN={args.domain}\n"
               f"DEMO_PASSWORD_HASH='{generate_password_hash(password)}'\n"
               f"DEMO_SESSION_SECRET={secrets.token_hex(32)}\n")
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error(f"{args.output} already exists; it was not changed.")
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print(f"Created {args.output}. Keep this file private and out of version control.")


if __name__ == "__main__":
    main()

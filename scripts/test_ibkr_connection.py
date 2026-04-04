#!/usr/bin/env python3
# trading-system/scripts/test_ibkr_connection.py
#
# IBKR TWS / IB Gateway pre-flight check for Phase 4 paper trading.
#
# Checks:
#   1. TCP reachability of IB Gateway on 127.0.0.1:7497
#   2. Current config in GCP Secret Manager (ibkr-paper-host/port/account-id)
#   3. Paper mode guard — refuses to test the live port (7496)
#
# Optional: --set-account DU1234567
#   Writes the real paper account ID to Secret Manager.
#   Run once after logging into IB Gateway and finding your account ID.
#
# Pre-requisites (manual steps before this script will pass):
#   1. Download IB Gateway from:
#        https://www.interactivebrokers.com/en/trading/ibgateway-stable.html
#   2. Log in with your paper trading credentials.
#   3. Configure → API → Settings:
#        ✓  Enable ActiveX and Socket Clients
#        ✓  Socket port: 7497
#        ✓  Allow connections from localhost only
#   4. Note your paper account ID (shown at top of IB Gateway — format: DU1234567).
#   5. Run:  python3 scripts/test_ibkr_connection.py --set-account DU1234567
#
# Usage:
#   python3 scripts/test_ibkr_connection.py
#   python3 scripts/test_ibkr_connection.py --set-account DU1234567

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import os

GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")
PAPER_PORT = 7497
LIVE_PORT = 7496
CONNECT_TIMEOUT = 5  # seconds


def tcp_connect(host: str, port: int, timeout: int = CONNECT_TIMEOUT) -> tuple[bool, str]:
    """Attempt a TCP connection. Returns (success, message)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP connection to {host}:{port} succeeded"
    except ConnectionRefusedError:
        return False, f"Connection refused at {host}:{port} — IB Gateway not running or API not enabled"
    except TimeoutError:
        return False, f"Timed out connecting to {host}:{port} after {timeout}s"
    except OSError as e:
        return False, f"Connection error: {e}"


def read_secret(secret_id: str) -> str | None:
    """Read a GCP Secret Manager value. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={secret_id}", f"--project={GCP_PROJECT}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def write_secret(secret_id: str, value: str) -> bool:
    """Write a new version to a GCP Secret Manager secret."""
    try:
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "add", secret_id,
             "--data-file=-", f"--project={GCP_PROJECT}"],
            input=value, capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def print_step(label: str, ok: bool, detail: str = "") -> None:
    status = "✓" if ok else "✗"
    color = "\033[32m" if ok else "\033[31m"
    reset = "\033[0m"
    line = f"  {color}{status}{reset}  {label}"
    if detail:
        line += f"\n       {detail}"
    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IBKR IB Gateway pre-flight check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--set-account",
        metavar="DU_ID",
        help="Write the real paper account ID to Secret Manager (e.g. DU1234567)",
    )
    args = parser.parse_args()

    print("\n═══════════════════════════════════════════════════════")
    print(" IBKR IB Gateway — Phase 4 Pre-flight Check")
    print("═══════════════════════════════════════════════════════\n")

    # ── Safety: never test live port ─────────────────────────────────────────
    port_secret = read_secret("ibkr-paper-port")
    configured_port = int(port_secret) if port_secret and port_secret.isdigit() else PAPER_PORT

    if configured_port == LIVE_PORT:
        print("  \033[31m✗  SAFETY ABORT: ibkr-paper-port is set to 7496 (live port).\033[0m")
        print("     This script only tests paper trading (7497). Never use 7496 here.")
        sys.exit(2)

    # ── Read current config from Secret Manager ───────────────────────────────
    print("  GCP Secret Manager config:")
    host_secret = read_secret("ibkr-paper-host")
    account_secret = read_secret("ibkr-account-id")

    host = host_secret or "127.0.0.1"
    port = configured_port
    account_id = account_secret or "DU_PLACEHOLDER"

    print_step(f"ibkr-paper-host  = {host}", host_secret is not None,
               "" if host_secret else "(Secret Manager unavailable — using default)")
    print_step(f"ibkr-paper-port  = {port}", True)
    is_placeholder = account_id in ("DU_PLACEHOLDER", "", None)
    print_step(
        f"ibkr-account-id  = {account_id}",
        not is_placeholder,
        "⚠  Not set — run with --set-account DU<digits> after logging into IB Gateway"
        if is_placeholder else "",
    )
    print()

    # ── Optional: update account ID ──────────────────────────────────────────
    if args.set_account:
        account_input = args.set_account.strip()
        if not (account_input.startswith("DU") and account_input[2:].isdigit()):
            print(f"  \033[31m✗  Invalid account ID format: '{account_input}'.\033[0m")
            print("     Paper account IDs start with 'DU' followed by digits (e.g. DU1234567).")
            sys.exit(1)

        print(f"  Writing ibkr-account-id = {account_input} to Secret Manager…")
        ok = write_secret("ibkr-account-id", account_input)
        if ok:
            print(f"  \033[32m✓  Secret Manager updated: ibkr-account-id = {account_input}\033[0m")
            account_id = account_input
            is_placeholder = False
        else:
            print("  \033[31m✗  Failed to write to Secret Manager.")
            print("     Ensure gcloud is authenticated: gcloud auth application-default login\033[0m")
        print()

    # ── TCP connectivity test ─────────────────────────────────────────────────
    print(f"  TCP connectivity to {host}:{port}:")
    ok, msg = tcp_connect(host, port)
    print_step(msg, ok)
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    if ok and not is_placeholder:
        print("  \033[32m✓  ALL CHECKS PASSED — IB Gateway reachable, account ID set.\033[0m")
        print("     The Rust engine will connect to IB Gateway when started with TRADING_MODE=paper.")
    elif ok and is_placeholder:
        print("  \033[33m⚠  TCP OK but account ID is DU_PLACEHOLDER.\033[0m")
        print("     Find your paper account ID in IB Gateway (top-right corner) and run:")
        print(f"     python3 scripts/test_ibkr_connection.py --set-account DU<digits>")
    else:
        print("  \033[31m✗  IB Gateway not reachable on 127.0.0.1:7497.\033[0m")
        print()
        print("  Setup steps:")
        print("   1. Install IB Gateway (not TWS — Gateway is headless/stable):")
        print("      https://www.interactivebrokers.com/en/trading/ibgateway-stable.html")
        print("   2. Log in with your paper trading username + password.")
        print("   3. Configure → API → Settings:")
        print("        ✓ Enable ActiveX and Socket Clients")
        print("        ✓ Socket port: 7497")
        print("        ✓ Allow connections from localhost only")
        print("   4. Note your account ID (shown at top, format DU1234567) then run:")
        print("      python3 scripts/test_ibkr_connection.py --set-account DU<digits>")

    print()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

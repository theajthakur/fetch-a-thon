"""Send testnet FET to the MediTwin Payment Agent to test the fet_direct payment flow.

Usage (needs: pip install cosmpy):
    python tools/send_test_fet.py fetch1...<payment agent wallet> [amount_fet=0.1]

- Creates (or reuses) a local TEST wallet saved in .test_fet_wallet.key (git-ignored; never use it for real funds).
- Tops it up from the Fetch.ai testnet faucet if its balance is too low.
- Sends the amount to the given address and prints the transaction hash for:
      @supply-chain-action verify tx <hash>      (or @meditwin-payment verify tx <hash>)
Testnet FET has no real value.
"""
import os
import sys
import time

from cosmpy.aerial.client import LedgerClient, NetworkConfig
from cosmpy.aerial.faucet import FaucetApi
from cosmpy.aerial.wallet import LocalWallet
from cosmpy.crypto.address import Address
from cosmpy.crypto.keypairs import PrivateKey

DENOM = "atestfet"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".test_fet_wallet.key")


def load_wallet() -> LocalWallet:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as fh:
            return LocalWallet(PrivateKey(fh.read().strip()))
    key = PrivateKey()
    with open(KEY_FILE, "w") as fh:
        fh.write(key.private_key)
    return LocalWallet(key)


def main():
    if len(sys.argv) < 2 or not sys.argv[1].startswith("fetch1"):
        sys.exit("Usage: python tools/send_test_fet.py fetch1...<payment agent wallet> [amount_fet]")
    recipient = sys.argv[1]
    amount = int(round(float(sys.argv[2] if len(sys.argv) > 2 else "0.1") * 10 ** 18))

    net = NetworkConfig.fetchai_stable_testnet()
    ledger = LedgerClient(net)
    wallet = load_wallet()
    print("Test wallet:", wallet.address())

    balance = ledger.query_bank_balance(wallet.address(), denom=DENOM)
    if balance < amount * 2:
        print("Requesting testnet FET from the faucet ...")
        FaucetApi(net).get_wealth(wallet.address())
        for _ in range(30):
            time.sleep(2)
            balance = ledger.query_bank_balance(wallet.address(), denom=DENOM)
            if balance >= amount * 2:
                break
    print(f"Balance: {balance / 10 ** 18:g} FET")
    if balance < amount * 2:
        sys.exit("Not enough testnet FET (faucet may be busy). Try again in a minute.")

    tx = ledger.send_tokens(Address(recipient), amount, DENOM, wallet).wait_to_complete()
    print(f"Sent {amount / 10 ** 18:g} FET to {recipient}")
    print("Transaction hash:", tx.tx_hash)
    print(f"\nNow send in ASI:One:\n  @supply-chain-action verify tx {tx.tx_hash}")


if __name__ == "__main__":
    main()

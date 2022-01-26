import json
import os
import requests

from brownie import interface

from helpers.addresses import registry
from rich.console import Console
from brownie import web3

console = Console()


class Badger():
    """
    collection of all contracts and methods needed to interact with the badger
    system.
    """


    def __init__(self, safe):
        self.safe = safe

        # tokens
        self.badger = interface.IBadger(
            registry.eth.treasury_tokens.BADGER,
            owner=self.safe.account
        )

        # contracts
        self.tree = interface.IBadgerTreeV2(
            registry.eth.badger_wallets.badgertree,
            owner=self.safe.account
        )
        self.strat_bvecvx = interface.IVestedCvx(
            registry.eth.strategies['native.vestedCVX'],
            owner=self.safe.account
        )
        self.timelock = safe.contract(
            registry.eth.governance_timelock
        )

        # misc
        self.api_url = 'https://api.badger.finance/v2/'


    def claim_all(self):
        """
        note: badger tree checks if `cycle` passed is equal to latest cycle,
        if not it will revert. therefore call is very time-sensitive!
        """
        url = self.api_url + 'reward/tree/' + self.safe.address

        # grab args from api endpoint
        response = requests.get(url)
        json_data = response.json()

        amounts_claimable = self.tree.getClaimableFor(
            self.safe.address,
            json_data['tokens'],
            json_data['cumulativeAmounts'],
        )[1]

        self.tree.claim(
            json_data['tokens'],
            json_data['cumulativeAmounts'],
            json_data['index'],
            json_data['cycle'],
            json_data['proof'],
            amounts_claimable,
        )


    def claim_bribes_votium(self, eligible_claims):
        """
        accepts a dict with `keys` being equal to the directory names used in
        the official votium repo (https://github.com/oo-00/Votium) and its
        `values` being the respective token's address.
        """
        # this does not leverage the `claimMulti` func yet but just loops
        for symbol, token_addr in eligible_claims.items():
            directory = 'data/Votium/merkle/'
            last_json = sorted(os.listdir(directory + symbol))[-1]
            with open(directory + symbol + f'/{last_json}') as f:
                leaf = json.load(f)['claims'][self.strat_bvecvx.address]
                self.strat_bvecvx.claimBribeFromVotium(
                    token_addr,
                    leaf['index'],
                    self.strat_bvecvx.address,
                    leaf['amount'],
                    leaf['proof']
                )
                
                
    def claim_bribes_convex(self, eligible_claims):
        """
        loop over `eligible_claims` dict to confirm if there are claimable
        rewards, and pass a list of claimable rewards to the strat to claim
        in one bulk tx.
        """
        self.safe.init_convex()
        claimables = []
        for token_addr in eligible_claims.values():
            claimable = self.safe.convex.cvx_extra_rewards.claimableRewards(
                self.strat_bvecvx.address, token_addr
            )
            if claimable > 0:
                claimables.append(token_addr)
        self.strat_bvecvx.claimBribesFromConvex(claimables)                

                
    def queue_timelock(self, target_addr, signature, data, dump_dir, delay_in_days=2.3):
        """
        Queue a call to `target_addr` with `signature` containing `data` into the
        'timelock' contract. Delay is slightly over 48 hours by default.
        Example of `signature` and `data`:
        signature = 'approveStrategy(address,address)'
        data = eth_abi.encode_abi(
            ['address', 'address'],
            [addr_var1, addr_var2],
        )
        """

        # calc timestamp of execution
        delay = int(delay_in_days * 60 * 60 * 24)
        eta = web3.eth.getBlock('latest')['timestamp'] + delay

        # queue actual action to the timelock
        tx = self.timelock.queueTransaction(target_addr, 0, signature, data, eta)

        # dump tx details to json file
        filename = tx.events['QueueTransaction']['txHash']
        console.print(f"Dump Directory: {dump_dir}")
        os.makedirs(dump_dir, exist_ok=True)
        with open(f'{dump_dir}{filename}.json', 'w') as f:
            tx_data = {
                'target': target_addr,
                'eth': 0,
                'signature': signature,
                'data': data.hex(),
                'eta': eta,
            }
            json.dump(tx_data, f, indent=4, sort_keys=True)


    def execute_timelock(self, queueTx_dir):
        """
        Loops through all the JSON files within the given 'queueTx_dir' and executes
        the txs that have already been queued on the 'timelock'.
        """
        path = os.path.dirname(queueTx_dir)
        directory = os.fsencode(path)

        for file in os.listdir(directory):
            filename = os.fsdecode(file)
            if not filename.endswith('.json'):
                continue
            txHash = filename.replace(".json", "")

            if self.timelock.queuedTransactions(txHash) == True:
                with open(f"{queueTx_dir}{filename}") as f:
                    tx = json.load(f)

                console.print(f"[green]Executing tx with parameters:[/green] {tx}")

                self.timelock.executeTransaction(
                    tx['target'], 0, tx['signature'], tx['data'], tx['eta']
                )
            else:
                with open(f"{queueTx_dir}{filename}") as f:
                    tx = json.load(f)
                console.print(f"[red]Tx not yet queued:[/red] {tx}")


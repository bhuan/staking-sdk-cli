#!/usr/bin/env python3
"""
Staking Events Listener
Connects to a Monad node via WebSocket and listens for all staking precompile events.
Displays events in a human-readable format in the terminal.
"""

import sys
import os
import argparse
import asyncio
import toml
from web3 import Web3, AsyncWeb3
from web3.providers.persistent import WebSocketProvider
from eth_abi import decode
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from datetime import datetime

# Staking precompile address
STAKING_CONTRACT_ADDRESS = "0x0000000000000000000000000000000000001000"

# Event signatures (keccak256 hash of event signature)
EVENT_SIGNATURES = {
    "ValidatorRewarded": "0x" + Web3.keccak(text="ValidatorRewarded(uint64,address,uint256,uint64)").hex(),
    "ValidatorCreated": "0x" + Web3.keccak(text="ValidatorCreated(uint64,address,uint256)").hex(),
    "ValidatorStatusChanged": "0x" + Web3.keccak(text="ValidatorStatusChanged(uint64,address,uint64)").hex(),
    "Delegate": "0x" + Web3.keccak(text="Delegate(uint64,address,uint256,uint64)").hex(),
    "Undelegate": "0x" + Web3.keccak(text="Undelegate(uint64,address,uint8,uint256,uint64)").hex(),
    "Withdraw": "0x" + Web3.keccak(text="Withdraw(uint64,address,uint8,uint256,uint64)").hex(),
    "ClaimRewards": "0x" + Web3.keccak(text="ClaimRewards(uint64,address,uint256,uint64)").hex(),
    "CommissionChanged": "0x" + Web3.keccak(text="CommissionChanged(uint64,uint256,uint256)").hex(),
    "EpochChanged": "0x" + Web3.keccak(text="EpochChanged(uint256,uint256)").hex(),
}

# Reverse mapping for quick lookup
SIGNATURE_TO_EVENT = {v: k for k, v in EVENT_SIGNATURES.items()}


class StakingEventListener:
    def __init__(self, ws_url: str, console: Console):
        """
        Initialize the event listener.

        Args:
            ws_url: WebSocket URL of the Monad node
            console: Rich console for output
        """
        self.ws_url = ws_url
        self.console = console
        self.event_count = 0
        self.events_history = []
        self.max_history = 100  # Keep last 100 events

    def decode_indexed_param(self, param_type: str, topic) -> any:
        """Decode an indexed parameter from a topic."""
        # Handle both string and HexBytes formats
        if isinstance(topic, str):
            topic_hex = topic
        else:
            topic_hex = topic.hex()

        # Ensure 0x prefix
        if not topic_hex.startswith('0x'):
            topic_hex = '0x' + topic_hex

        topic_bytes = bytes.fromhex(topic_hex[2:])  # Remove '0x' prefix

        if param_type == "uint64":
            # uint64 is padded to 32 bytes
            return int.from_bytes(topic_bytes, byteorder='big')
        elif param_type == "address":
            # Address is in the last 20 bytes
            return Web3.to_checksum_address("0x" + topic_bytes[-20:].hex())
        elif param_type == "uint256":
            return int.from_bytes(topic_bytes, byteorder='big')
        else:
            return topic_hex

    def decode_event(self, log) -> dict:
        """
        Decode a staking event log.

        Args:
            log: The log entry from the blockchain

        Returns:
            Dictionary containing decoded event data
        """
        # Check if topics exist and have content
        if 'topics' not in log or not log['topics'] or len(log['topics']) == 0:
            self.console.print(f"[yellow]Debug: Log has no topics. Log keys: {log.keys()}[/yellow]")
            return None

        # Get event signature from first topic
        topic0 = log['topics'][0]
        # Handle both hex string and HexBytes formats
        if isinstance(topic0, str):
            event_sig = topic0
        else:
            event_sig = topic0.hex()

        # Ensure it has 0x prefix
        if not event_sig.startswith('0x'):
            event_sig = '0x' + event_sig

        event_name = SIGNATURE_TO_EVENT.get(event_sig)

        if not event_name:
            self.console.print(f"[yellow]Debug: Unknown event signature: {event_sig}[/yellow]")
            self.console.print(f"[yellow]Debug: Known signatures: {list(SIGNATURE_TO_EVENT.keys())[:3]}...[/yellow]")
            return None

        # Handle transactionHash - can be hex string or HexBytes
        tx_hash = log.get('transactionHash', '')
        if isinstance(tx_hash, str):
            tx_hash_hex = tx_hash
        else:
            tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)

        decoded_event = {
            'name': event_name,
            'blockNumber': log.get('blockNumber', 0),
            'transactionHash': tx_hash_hex,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        # Get data field - handle both hex string and HexBytes
        log_data = log.get('data', '0x')
        if isinstance(log_data, str):
            data_hex = log_data
        else:
            data_hex = log_data.hex() if hasattr(log_data, 'hex') else '0x'

        # Ensure data has 0x prefix
        if not data_hex.startswith('0x'):
            data_hex = '0x' + data_hex

        # Decode based on event type
        try:
            if event_name == "ValidatorRewarded":
                # event ValidatorRewarded(uint64 indexed valId, address indexed from, uint256 amount, uint64 epoch)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['from'] = self.decode_indexed_param("address", log['topics'][2])
                # Non-indexed parameters are in data
                amount, epoch = decode(['uint256', 'uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['amount'] = Web3.from_wei(amount, 'ether')
                decoded_event['epoch'] = epoch

            elif event_name == "ValidatorCreated":
                # event ValidatorCreated(uint64 indexed validatorId, address indexed authAddress, uint256 commission)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['authAddress'] = self.decode_indexed_param("address", log['topics'][2])
                commission, = decode(['uint256'], bytes.fromhex(data_hex[2:]))
                decoded_event['commission'] = commission / 1e18 * 100  # Convert to percentage

            elif event_name == "ValidatorStatusChanged":
                # event ValidatorStatusChanged(uint64 indexed validatorId, address indexed authAddress, uint64 flags)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['authAddress'] = self.decode_indexed_param("address", log['topics'][2])
                flags, = decode(['uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['flags'] = flags

            elif event_name == "Delegate":
                # event Delegate(uint64 indexed validatorId, address indexed delegator, uint256 amount, uint64 activationEpoch)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['delegator'] = self.decode_indexed_param("address", log['topics'][2])
                amount, activation_epoch = decode(['uint256', 'uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['amount'] = Web3.from_wei(amount, 'ether')
                decoded_event['activationEpoch'] = activation_epoch

            elif event_name == "Undelegate":
                # event Undelegate(uint64 indexed validatorId, address indexed delegator, uint8 withdrawId, uint256 amount, uint64 activationEpoch)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['delegator'] = self.decode_indexed_param("address", log['topics'][2])
                withdraw_id, amount, activation_epoch = decode(['uint8', 'uint256', 'uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['withdrawId'] = withdraw_id
                decoded_event['amount'] = Web3.from_wei(amount, 'ether')
                decoded_event['activationEpoch'] = activation_epoch

            elif event_name == "Withdraw":
                # event Withdraw(uint64 indexed validatorId, address indexed delegator, uint8 withdrawId, uint256 amount, uint64 withdrawEpoch)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['delegator'] = self.decode_indexed_param("address", log['topics'][2])
                withdraw_id, amount, withdraw_epoch = decode(['uint8', 'uint256', 'uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['withdrawId'] = withdraw_id
                decoded_event['amount'] = Web3.from_wei(amount, 'ether')
                decoded_event['withdrawEpoch'] = withdraw_epoch

            elif event_name == "ClaimRewards":
                # event ClaimRewards(uint64 indexed validatorId, address indexed delegator, uint256 amount, uint64 epoch)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                decoded_event['delegator'] = self.decode_indexed_param("address", log['topics'][2])
                amount, epoch = decode(['uint256', 'uint64'], bytes.fromhex(data_hex[2:]))
                decoded_event['amount'] = Web3.from_wei(amount, 'ether')
                decoded_event['epoch'] = epoch

            elif event_name == "CommissionChanged":
                # event CommissionChanged(uint64 indexed validatorId, uint256 oldCommission, uint256 newCommission)
                decoded_event['validatorId'] = self.decode_indexed_param("uint64", log['topics'][1])
                old_commission, new_commission = decode(['uint256', 'uint256'], bytes.fromhex(data_hex[2:]))
                decoded_event['oldCommission'] = old_commission / 1e18 * 100
                decoded_event['newCommission'] = new_commission / 1e18 * 100

            elif event_name == "EpochChanged":
                # event EpochChanged(uint256 oldEpoch, uint256 newEpoch)
                old_epoch, new_epoch = decode(['uint256', 'uint256'], bytes.fromhex(data_hex[2:]))
                decoded_event['oldEpoch'] = old_epoch
                decoded_event['newEpoch'] = new_epoch

        except Exception as e:
            self.console.print(f"[red]Error decoding event {event_name}: {e}[/red]")
            return None

        return decoded_event

    def format_event(self, event: dict) -> Panel:
        """
        Format an event as a Rich panel for display.

        Args:
            event: Decoded event dictionary

        Returns:
            Rich Panel object
        """
        event_name = event['name']

        # Choose color based on event type
        colors = {
            'ValidatorRewarded': 'green',
            'ValidatorCreated': 'blue',
            'ValidatorStatusChanged': 'yellow',
            'Delegate': 'cyan',
            'Undelegate': 'magenta',
            'Withdraw': 'red',
            'ClaimRewards': 'green',
            'CommissionChanged': 'yellow',
            'EpochChanged': 'bright_blue',
        }
        color = colors.get(event_name, 'white')

        # Build content based on event type
        lines = []
        lines.append(f"[bold]Block:[/bold] {event['blockNumber']}")
        lines.append(f"[bold]Time:[/bold] {event['timestamp']}")
        lines.append(f"[bold]Tx Hash:[/bold] {event['transactionHash']}")
        lines.append("")

        if event_name == "ValidatorRewarded":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]From:[/bold] {event['from']}")
            lines.append(f"[bold]Reward Amount:[/bold] {event['amount']:.6f} MON")
            lines.append(f"[bold]Epoch:[/bold] {event['epoch']}")

        elif event_name == "ValidatorCreated":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Auth Address:[/bold] {event['authAddress']}")
            lines.append(f"[bold]Commission:[/bold] {event['commission']:.2f}%")

        elif event_name == "ValidatorStatusChanged":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Auth Address:[/bold] {event['authAddress']}")
            lines.append(f"[bold]Flags:[/bold] {event['flags']}")

        elif event_name == "Delegate":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Delegator:[/bold] {event['delegator']}")
            lines.append(f"[bold]Amount:[/bold] {event['amount']:.6f} MON")
            lines.append(f"[bold]Activation Epoch:[/bold] {event['activationEpoch']}")

        elif event_name == "Undelegate":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Delegator:[/bold] {event['delegator']}")
            lines.append(f"[bold]Withdraw ID:[/bold] {event['withdrawId']}")
            lines.append(f"[bold]Amount:[/bold] {event['amount']:.6f} MON")
            lines.append(f"[bold]Activation Epoch:[/bold] {event['activationEpoch']}")

        elif event_name == "Withdraw":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Delegator:[/bold] {event['delegator']}")
            lines.append(f"[bold]Withdraw ID:[/bold] {event['withdrawId']}")
            lines.append(f"[bold]Amount:[/bold] {event['amount']:.6f} MON")
            lines.append(f"[bold]Withdraw Epoch:[/bold] {event['withdrawEpoch']}")

        elif event_name == "ClaimRewards":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Delegator:[/bold] {event['delegator']}")
            lines.append(f"[bold]Amount:[/bold] {event['amount']:.6f} MON")
            lines.append(f"[bold]Epoch:[/bold] {event['epoch']}")

        elif event_name == "CommissionChanged":
            lines.append(f"[bold]Validator ID:[/bold] {event['validatorId']}")
            lines.append(f"[bold]Old Commission:[/bold] {event['oldCommission']:.2f}%")
            lines.append(f"[bold]New Commission:[/bold] {event['newCommission']:.2f}%")

        elif event_name == "EpochChanged":
            lines.append(f"[bold]Old Epoch:[/bold] {event['oldEpoch']}")
            lines.append(f"[bold]New Epoch:[/bold] {event['newEpoch']}")

        content = "\n".join(lines)

        return Panel(
            content,
            title=f"[{color} bold]{event_name}[/{color} bold]",
            border_style=color,
            padding=(1, 2)
        )

    async def listen_for_events(self):
        """
        Main event listening loop.
        Subscribes to logs and processes them in real-time using AsyncWeb3.
        """
        self.console.print(f"[cyan]Connecting to {self.ws_url}...[/cyan]")
        self.console.print(f"\n[bold cyan]Starting event listener for staking contract: {STAKING_CONTRACT_ADDRESS}[/bold cyan]")
        self.console.print("[yellow]Press Ctrl+C to stop[/yellow]\n")

        # Create subscription filter - only logs from staking contract
        filter_params = {
            'address': STAKING_CONTRACT_ADDRESS,
        }

        try:
            # Use AsyncWeb3 with persistent WebSocket provider
            async with AsyncWeb3(WebSocketProvider(self.ws_url)) as w3:
                self.console.print("[green]✓ Connected to Monad node[/green]")

                # Get current block number
                try:
                    current_block = await w3.eth.block_number
                    self.console.print(f"[cyan]Current block: {current_block}[/cyan]\n")
                except Exception as e:
                    self.console.print(f"[yellow]Note: Could not fetch block number: {e}[/yellow]\n")

                # Subscribe to logs
                subscription_id = await w3.eth.subscribe('logs', filter_params)
                self.console.print(f"[green]✓ Subscribed to staking events (subscription ID: {subscription_id})[/green]\n")

                # Process subscription events
                async for payload in w3.socket.process_subscriptions():
                    # Extract the log from the payload
                    if 'result' not in payload:
                        continue

                    log = payload['result']
                    self.event_count += 1

                    # Decode the event
                    decoded_event = self.decode_event(log)

                    if decoded_event:
                        # Store in history
                        self.events_history.append(decoded_event)
                        if len(self.events_history) > self.max_history:
                            self.events_history.pop(0)

                        # Display the event
                        panel = self.format_event(decoded_event)
                        self.console.print(panel)
                        self.console.print(f"[dim]Total events received: {self.event_count}[/dim]\n")
                    else:
                        self.console.print(f"[dim]Received unknown event or failed to decode[/dim]")

        except KeyboardInterrupt:
            self.console.print("\n[yellow]Stopping event listener...[/yellow]")
        except Exception as e:
            self.console.print(f"[red]Error in event listener: {e}[/red]")
            import traceback
            traceback.print_exc()


def read_config(config_path: str) -> dict:
    """Read configuration from TOML file."""
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        return toml.load(f)


async def main():
    parser = argparse.ArgumentParser(
        description="Listen to Monad staking events over WebSocket"
    )
    parser.add_argument(
        "--ws-url",
        type=str,
        help="WebSocket URL of the Monad node (e.g., wss://node.example.com)"
    )
    parser.add_argument(
        "--config-path",
        type=str,
        default="./config.toml",
        help="Path to config.toml file (default: ./config.toml)"
    )

    args = parser.parse_args()

    console = Console()

    # Display banner
    banner = """
    ╔═══════════════════════════════════════════════════════╗
    ║         Monad Staking Events Listener                ║
    ║         Real-time WebSocket Event Monitor            ║
    ╚═══════════════════════════════════════════════════════╝
    """
    console.print(banner, style="bold cyan")

    # Determine WebSocket URL
    ws_url = args.ws_url

    if not ws_url:
        # Try to read from config
        try:
            config = read_config(args.config_path)
            rpc_url = config.get('rpc_url', '')

            # Convert HTTP(S) URL to WebSocket URL
            if rpc_url.startswith('https://'):
                ws_url = rpc_url.replace('https://', 'wss://')
            elif rpc_url.startswith('http://'):
                ws_url = rpc_url.replace('http://', 'ws://')
            else:
                ws_url = rpc_url

            if not ws_url.startswith('ws://') and not ws_url.startswith('wss://'):
                console.print("[red]Error: Invalid WebSocket URL[/red]")
                console.print("Please provide a WebSocket URL using --ws-url or configure rpc_url in config.toml")
                sys.exit(1)

        except FileNotFoundError:
            console.print(f"[red]Error: Config file not found at {args.config_path}[/red]")
            console.print("Please provide a WebSocket URL using --ws-url")
            sys.exit(1)

    # Create listener and start
    listener = StakingEventListener(ws_url, console)
    await listener.listen_for_events()


if __name__ == "__main__":
    asyncio.run(main())

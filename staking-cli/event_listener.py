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
import json
import csv
import logging
from collections import deque
from web3 import Web3, AsyncWeb3
from web3.providers.persistent import WebSocketProvider
from eth_abi import decode
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import asyncpg
from decimal import Decimal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

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
    def __init__(self, ws_url: str, console: Console, speculative: bool = False,
                 export_json: str = None, export_csv: str = None,
                 filter_validator_id: int = None, filter_address: str = None,
                 db_config: dict = None):
        """
        Initialize the event listener.

        Args:
            ws_url: WebSocket URL of the Monad node
            console: Rich console for output
            speculative: Use monadLogs for speculative execution (~1s faster, pre-finalization)
            export_json: Path to JSON file for exporting events
            export_csv: Path to CSV file for exporting events
            filter_validator_id: Only show events for this validator ID
            filter_address: Only show events involving this address (delegator/validator)
            db_config: Database configuration dict (host, port, database, user, password)
        """
        self.ws_url = ws_url
        self.console = console
        self.speculative = speculative
        self.export_json = export_json
        self.export_csv = export_csv
        self.filter_validator_id = filter_validator_id
        self.filter_address = filter_address.lower() if filter_address else None
        self.db_config = db_config
        self.db_pool: Optional[asyncpg.Pool] = None
        self.event_count = 0
        self.filtered_count = 0
        self.events_history = []
        self.max_history = 100  # Keep last 100 events

        # Track seen events to avoid duplicates in speculative mode
        # Using deque to maintain insertion order for proper cache eviction
        self.seen_events = deque(maxlen=10000)  # Auto-evicts oldest when full
        self.seen_events_set = set()  # Fast lookup

        # Track last processed block for catch-up on reconnection
        self.last_block_number = None

        # Initialize export files
        if self.export_csv:
            self._init_csv_file()

    def _init_csv_file(self):
        """Initialize CSV file with headers."""
        try:
            with open(self.export_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'Timestamp', 'Event Name', 'Block Number', 'Transaction Hash',
                    'Validator ID', 'Address', 'Amount', 'Epoch', 'Additional Data'
                ])
            self.console.print(f"[green]✓ CSV export initialized: {self.export_csv}[/green]")
        except Exception as e:
            self.console.print(f"[red]Error initializing CSV file: {e}[/red]")
            self.export_csv = None

    def _get_event_key(self, event: dict) -> tuple:
        """Generate a unique key for deduplication."""
        # Create a key from block, tx hash, event name, and key data fields
        # This handles duplicates from speculative execution state transitions
        key_data = []

        # Add validator ID if present
        if 'validatorId' in event:
            key_data.append(('validatorId', event['validatorId']))

        # Add addresses
        for field in ['authAddress', 'delegator', 'from']:
            if field in event and event[field]:
                key_data.append((field, event[field].lower()))

        # Add amount and epoch info
        for field in ['amount', 'epoch', 'activationEpoch', 'withdrawEpoch', 'withdrawId']:
            if field in event:
                key_data.append((field, str(event[field])))

        # Add commission info
        for field in ['commission', 'oldCommission', 'newCommission']:
            if field in event:
                key_data.append((field, str(event[field])))

        return (
            event.get('blockNumber'),
            event.get('transactionHash'),
            event.get('name'),
            tuple(sorted(key_data))
        )

    def _is_duplicate_event(self, event: dict) -> bool:
        """Check if we've already seen this event (for speculative mode deduplication)."""
        event_key = self._get_event_key(event)
        if event_key in self.seen_events_set:
            return True

        # Add to both deque and set
        self.seen_events.append(event_key)
        self.seen_events_set.add(event_key)

        # When deque is full, it auto-evicts the oldest item
        # We need to sync the set by removing evicted items
        if len(self.seen_events_set) > len(self.seen_events):
            # Rebuild set from deque to remove stale entries
            self.seen_events_set = set(self.seen_events)

        return False

    def _should_display_event(self, event: dict) -> bool:
        """Check if event matches filter criteria."""
        # If no filters, display all events
        if not self.filter_validator_id and not self.filter_address:
            return True

        # Filter by validator ID
        if self.filter_validator_id:
            event_validator_id = event.get('validatorId')
            if event_validator_id is None or event_validator_id != self.filter_validator_id:
                return False

        # Filter by address
        if self.filter_address:
            # Check all address fields
            event_addresses = [
                event.get('authAddress', '').lower(),
                event.get('delegator', '').lower(),
                event.get('from', '').lower(),
            ]

            if self.filter_address not in event_addresses:
                return False

        return True

    async def _init_db_pool(self):
        """Initialize database connection pool and get last processed block."""
        if not self.db_config:
            return

        try:
            self.db_pool = await asyncpg.create_pool(
                host=self.db_config.get('host', 'localhost'),
                port=self.db_config.get('port', 5432),
                database=self.db_config.get('database', 'monad_events'),
                user=self.db_config.get('user', 'postgres'),
                password=self.db_config.get('password', ''),
                min_size=2,
                max_size=10,
                command_timeout=60
            )
            self.console.print(f"[green]✓ Database connection pool initialized[/green]")

            # Get last processed block from database for catch-up on restart
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchval('SELECT MAX(block_number) FROM staking_events')
                if result:
                    self.last_block_number = result
                    self.console.print(f"[cyan]Last block in database: {result}[/cyan]")
                else:
                    self.console.print(f"[dim]Database is empty, starting fresh[/dim]")

        except Exception as e:
            self.console.print(f"[red]Error connecting to database: {e}[/red]")
            self.console.print(f"[yellow]Continuing without database storage...[/yellow]")
            self.db_pool = None

    async def _close_db_pool(self):
        """Close database connection pool."""
        if self.db_pool:
            await self.db_pool.close()
            self.console.print(f"[cyan]Database connection pool closed[/cyan]")

    async def _save_event_to_db(self, event: dict):
        """Save event to PostgreSQL database."""
        if not self.db_pool:
            return

        try:
            # Extract common fields
            validator_id = event.get('validatorId')
            address = event.get('authAddress') or event.get('delegator') or event.get('from')
            amount = event.get('amount')
            epoch = event.get('epoch') or event.get('activationEpoch') or event.get('withdrawEpoch')

            # Convert amount to wei (stored as Decimal in DB)
            amount_wei = None
            if amount is not None:
                # Amount is in MON (ether), convert to wei
                amount_wei = Decimal(str(amount)) * Decimal('1000000000000000000')

            # Collect additional event-specific data
            event_data = {}
            for key, value in event.items():
                if key not in ['name', 'blockNumber', 'transactionHash', 'timestamp',
                              'validatorId', 'authAddress', 'delegator', 'from',
                              'amount', 'epoch', 'activationEpoch', 'withdrawEpoch']:
                    # Convert Decimal to string for JSON storage
                    if isinstance(value, (Decimal, float)):
                        event_data[key] = str(value)
                    else:
                        event_data[key] = value

            # Insert into database
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO staking_events (
                        timestamp, event_name, block_number, transaction_hash,
                        validator_id, address, amount, epoch, event_data
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT DO NOTHING
                ''',
                    datetime.now(timezone.utc),
                    event['name'],
                    event['blockNumber'],
                    event['transactionHash'],
                    validator_id,
                    address.lower() if address else None,
                    amount_wei,
                    epoch,
                    json.dumps(event_data) if event_data else None
                )
        except Exception as e:
            self.console.print(f"[red]Error saving to database: {e}[/red]")

    async def _export_event(self, event: dict):
        """Export event to configured export formats."""
        # Save to database
        if self.db_pool:
            await self._save_event_to_db(event)

        # Export to JSON (append mode)
        if self.export_json:
            try:
                # Read existing data
                if Path(self.export_json).exists():
                    with open(self.export_json, 'r') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError:
                            data = []
                else:
                    data = []

                # Append new event
                data.append(event)

                # Write back
                with open(self.export_json, 'w') as f:
                    json.dump(data, f, indent=2, default=str)
            except Exception as e:
                self.console.print(f"[red]Error exporting to JSON: {e}[/red]")

        # Export to CSV (append mode)
        if self.export_csv:
            try:
                with open(self.export_csv, 'a', newline='') as f:
                    writer = csv.writer(f)

                    # Extract common fields
                    validator_id = event.get('validatorId', '')
                    address = event.get('authAddress') or event.get('delegator') or event.get('from', '')
                    amount = event.get('amount', '')
                    epoch = event.get('epoch') or event.get('activationEpoch') or event.get('withdrawEpoch', '')

                    # Collect additional data
                    additional = {}
                    for key, value in event.items():
                        if key not in ['name', 'blockNumber', 'transactionHash', 'timestamp',
                                      'validatorId', 'authAddress', 'delegator', 'from',
                                      'amount', 'epoch', 'activationEpoch', 'withdrawEpoch']:
                            additional[key] = value

                    writer.writerow([
                        event.get('timestamp', ''),
                        event.get('name', ''),
                        event.get('blockNumber', ''),
                        event.get('transactionHash', ''),
                        validator_id,
                        address,
                        amount,
                        epoch,
                        json.dumps(additional) if additional else ''
                    ])
            except Exception as e:
                self.console.print(f"[red]Error exporting to CSV: {e}[/red]")

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
            logging.warning(f"Log has no topics. Log keys: {log.keys()}")
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
            logging.debug(f"Unknown event signature: {event_sig}")
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
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
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

    async def _catchup_missed_events(self, w3, filter_params: dict):
        """
        Fetch missed events since last processed block.
        Uses chunked requests (10 blocks at a time) to respect Monad's eth_getLogs limitations.
        """
        if not self.last_block_number:
            return  # First connection, no catch-up needed

        try:
            current_block = await w3.eth.block_number

            if current_block > self.last_block_number:
                missed_blocks = current_block - self.last_block_number
                self.console.print(f"[yellow]⚠ Catching up {missed_blocks} missed blocks ({self.last_block_number} → {current_block})[/yellow]")

                # Chunk size: 10 blocks per request (Monad recommendation for optimal performance)
                chunk_size = 10
                total_events_found = 0

                # Process in chunks
                from_block = self.last_block_number + 1
                while from_block <= current_block:
                    to_block = min(from_block + chunk_size - 1, current_block)

                    try:
                        # Fetch historical logs for this chunk
                        historical_filter = {
                            'address': filter_params['address'],
                            'fromBlock': from_block,
                            'toBlock': to_block
                        }

                        logs = await w3.eth.get_logs(historical_filter)

                        if logs:
                            total_events_found += len(logs)
                            self.console.print(f"[dim]Chunk {from_block}-{to_block}: {len(logs)} events[/dim]")

                            for log in logs:
                                self.event_count += 1  # Count all events received

                                # Process each missed event
                                decoded_event = self.decode_event(log)

                                if decoded_event:
                                    # Update last block number
                                    if decoded_event.get('blockNumber', 0) > (self.last_block_number or 0):
                                        self.last_block_number = decoded_event['blockNumber']

                                    # Check for duplicates
                                    if self._is_duplicate_event(decoded_event):
                                        continue

                                    # Check filters
                                    if self._should_display_event(decoded_event):
                                        self.filtered_count += 1

                                        # Export to DB/files
                                        await self._export_event(decoded_event)

                                        # Display (but more compact during catch-up)
                                        panel = self.format_event(decoded_event)
                                        self.console.print(panel)

                                        if self.filter_validator_id or self.filter_address:
                                            self.console.print(f"[dim]Events displayed: {self.filtered_count} | Total received: {self.event_count}[/dim]\n")
                                        else:
                                            self.console.print(f"[dim]Total events received: {self.event_count}[/dim]\n")

                    except Exception as chunk_error:
                        self.console.print(f"[yellow]Error fetching chunk {from_block}-{to_block}: {chunk_error}[/yellow]")
                        # Continue with next chunk even if one fails

                    # Move to next chunk
                    from_block = to_block + 1

                if total_events_found > 0:
                    self.console.print(f"[green]✓ Catch-up complete: processed {total_events_found} events from {missed_blocks} blocks[/green]\n")
                else:
                    self.console.print(f"[dim]No events during downtime ({missed_blocks} blocks checked)[/dim]\n")

        except Exception as e:
            self.console.print(f"[yellow]Warning: Could not catch up missed events: {e}[/yellow]")
            self.console.print(f"[yellow]Continuing with real-time monitoring from current block[/yellow]\n")

    async def _connect_and_listen(self, filter_params: dict, subscription_type: str):
        """
        Internal method to establish connection and process events.
        """
        last_event_time = asyncio.get_event_loop().time()
        timeout_seconds = 300  # 5 minutes without events = reconnect

        async with AsyncWeb3(WebSocketProvider(self.ws_url)) as w3:
            self.console.print("[green]✓ Connected to Monad node[/green]")

            # Subscribe IMMEDIATELY to start receiving real-time events
            subscription_id = await w3.eth.subscribe(subscription_type, filter_params)
            self.console.print(f"[green]✓ Subscribed to staking events (subscription ID: {subscription_id})[/green]")

            # Get current block number and catch up in background
            try:
                current_block = await w3.eth.block_number
                self.console.print(f"[cyan]Current block: {current_block}[/cyan]")

                # Start catch-up in background (non-blocking) with error handling
                # The deduplication system will handle any overlap between catch-up and real-time
                if self.last_block_number:
                    async def catchup_with_error_handling():
                        try:
                            await self._catchup_missed_events(w3, filter_params)
                        except Exception as e:
                            self.console.print(f"[red]Error during catch-up: {e}[/red]")
                            logging.exception("Catch-up failed with exception:")

                    asyncio.create_task(catchup_with_error_handling())
                else:
                    self.console.print(f"[dim]First connection, starting from current block[/dim]\n")

            except Exception as e:
                self.console.print(f"[yellow]Note: Could not fetch block number: {e}[/yellow]\n")

            # Process subscription events with timeout detection
            async def process_events():
                nonlocal last_event_time
                async for payload in w3.socket.process_subscriptions():
                    last_event_time = asyncio.get_event_loop().time()

                    # Extract the log from the payload
                    if 'result' not in payload:
                        continue

                    log = payload['result']
                    self.event_count += 1

                    # Decode the event
                    decoded_event = self.decode_event(log)

                    if decoded_event:
                        # Update last processed block number
                        block_num = decoded_event.get('blockNumber', 0)
                        if block_num > (self.last_block_number or 0):
                            self.last_block_number = block_num

                        # Check for duplicates (happens in speculative mode)
                        if self._is_duplicate_event(decoded_event):
                            # Silently skip duplicate - common in speculative execution
                            continue

                        # Check if event matches filters
                        if self._should_display_event(decoded_event):
                            self.filtered_count += 1

                            # Store in history
                            self.events_history.append(decoded_event)
                            if len(self.events_history) > self.max_history:
                                self.events_history.pop(0)

                            # Export event to files/database if configured
                            await self._export_event(decoded_event)

                            # Display the event
                            panel = self.format_event(decoded_event)
                            self.console.print(panel)

                            # Show count (filtered/total if filtering is active)
                            if self.filter_validator_id or self.filter_address:
                                self.console.print(f"[dim]Events displayed: {self.filtered_count} | Total received: {self.event_count}[/dim]\n")
                            else:
                                self.console.print(f"[dim]Total events received: {self.event_count}[/dim]\n")
                        # If event doesn't match filter, silently skip (don't spam console)
                    else:
                        self.console.print(f"[dim]Received unknown event or failed to decode[/dim]")

            # Watchdog to detect stuck connections
            async def watchdog():
                nonlocal last_event_time
                while True:
                    await asyncio.sleep(60)  # Check every minute
                    elapsed = asyncio.get_event_loop().time() - last_event_time
                    if elapsed > timeout_seconds:
                        raise TimeoutError(f"No events received for {int(elapsed)} seconds - connection appears dead")

            # Run both tasks concurrently
            try:
                await asyncio.gather(
                    process_events(),
                    watchdog()
                )
            except TimeoutError as e:
                self.console.print(f"[yellow]Watchdog timeout: {e}[/yellow]")
                raise

    async def listen_for_events(self):
        """
        Main event listening loop with automatic reconnection.
        Subscribes to logs and processes them in real-time using AsyncWeb3.
        """
        # Initialize database connection pool if configured
        await self._init_db_pool()

        self.console.print(f"[cyan]Connecting to {self.ws_url}...[/cyan]")
        self.console.print(f"\n[bold cyan]Starting event listener for staking contract: {STAKING_CONTRACT_ADDRESS}[/bold cyan]")

        # Determine subscription type
        subscription_type = 'monadLogs' if self.speculative else 'logs'
        mode_desc = "[yellow bold]SPECULATIVE MODE[/yellow bold] (~1s faster, pre-finalization)" if self.speculative else "[green]FINALIZED MODE[/green] (confirmed blocks)"
        self.console.print(f"Mode: {mode_desc}")
        self.console.print(f"[dim]Subscription type: {subscription_type}[/dim]")

        # Display active filters
        if self.filter_validator_id or self.filter_address:
            self.console.print("\n[bold yellow]Active Filters:[/bold yellow]")
            if self.filter_validator_id:
                self.console.print(f"  [dim]• Validator ID: {self.filter_validator_id}[/dim]")
            if self.filter_address:
                self.console.print(f"  [dim]• Address: {self.filter_address}[/dim]")

        self.console.print("[yellow]Press Ctrl+C to stop[/yellow]\n")

        # Create subscription filter - only logs from staking contract
        filter_params = {
            'address': STAKING_CONTRACT_ADDRESS,
        }

        # Reconnection settings
        max_retries = None  # Infinite retries
        retry_delay = 1  # Start with 1 second
        max_retry_delay = 60  # Max 60 seconds between retries
        retry_count = 0

        try:
            while True:
                try:
                    await self._connect_and_listen(filter_params, subscription_type)
                    # If we get here, connection closed normally
                    break
                except KeyboardInterrupt:
                    raise  # Propagate KeyboardInterrupt to outer handler
                except Exception as e:
                    retry_count += 1
                    # Calculate backoff delay with exponential backoff
                    delay = min(retry_delay * (2 ** (retry_count - 1)), max_retry_delay)

                    self.console.print(f"[yellow]Connection lost: {e}[/yellow]")
                    self.console.print(f"[cyan]Reconnecting in {delay} seconds... (attempt {retry_count})[/cyan]")

                    await asyncio.sleep(delay)

                    # Reset retry count on successful reconnection after some events
                    if retry_count > 5:
                        retry_count = max(0, retry_count - 1)

        except KeyboardInterrupt:
            self.console.print("\n[yellow]Stopping event listener...[/yellow]")
        except Exception as e:
            self.console.print(f"[red]Fatal error in event listener: {e}[/red]")
            logging.exception("Event listener crashed with exception:")
        finally:
            # Close database connection pool
            await self._close_db_pool()


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
    parser.add_argument(
        "--speculative",
        action="store_true",
        help="Use monadLogs for speculative execution (~1s faster, pre-finalization)"
    )
    parser.add_argument(
        "--export-json",
        type=str,
        help="Export events to JSON file (e.g., events.json)"
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        help="Export events to CSV file (e.g., events.csv)"
    )
    parser.add_argument(
        "--filter-validator-id",
        type=int,
        help="Only display events for specific validator ID"
    )
    parser.add_argument(
        "--filter-address",
        type=str,
        help="Only display events involving specific address (validator/delegator)"
    )
    parser.add_argument(
        "--db-host",
        type=str,
        help="Database host (default: from config or localhost)"
    )
    parser.add_argument(
        "--db-port",
        type=int,
        default=5432,
        help="Database port (default: 5432)"
    )
    parser.add_argument(
        "--db-name",
        type=str,
        default="monad_events",
        help="Database name (default: monad_events)"
    )
    parser.add_argument(
        "--db-user",
        type=str,
        help="Database user (default: from config or postgres)"
    )
    parser.add_argument(
        "--db-password",
        type=str,
        help="Database password (default: from config or empty)"
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

    # Build database configuration
    # Try from command line args, then from config file
    config = {}
    try:
        config = read_config(args.config_path)
    except FileNotFoundError:
        pass

    db_host = args.db_host or config.get('database', {}).get('host')
    db_user = args.db_user or config.get('database', {}).get('user')

    # Only create db_config if we have at least a host
    db_config = None
    if db_host:
        db_config = {
            'host': db_host,
            'port': args.db_port,
            'database': args.db_name,
            'user': db_user or 'postgres',
            'password': args.db_password or config.get('database', {}).get('password', ''),
        }

    # Create listener and start
    listener = StakingEventListener(
        ws_url,
        console,
        speculative=args.speculative,
        export_json=args.export_json,
        export_csv=args.export_csv,
        filter_validator_id=args.filter_validator_id,
        filter_address=args.filter_address,
        db_config=db_config
    )
    await listener.listen_for_events()


if __name__ == "__main__":
    asyncio.run(main())

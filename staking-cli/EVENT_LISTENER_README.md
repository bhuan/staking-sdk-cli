# Staking Events Listener

A real-time WebSocket event listener for monitoring all Monad staking precompile events.

## Overview

The event listener connects to a Monad node via WebSocket and monitors all events emitted by the staking precompile contract at `0x0000000000000000000000000000000000001000`. It decodes and displays events in a human-readable format with color-coded output.

## Monitored Events

The listener tracks all staking-related events:

1. **ValidatorRewarded** - When validators receive block rewards
2. **ValidatorCreated** - When new validators are registered
3. **ValidatorStatusChanged** - When validator flags/status change
4. **Delegate** - When stake is delegated to a validator
5. **Undelegate** - When stake is undelegated from a validator
6. **Withdraw** - When undelegated stake is withdrawn
7. **ClaimRewards** - When rewards are claimed
8. **CommissionChanged** - When validator commission is updated
9. **EpochChanged** - When the epoch changes

## Requirements

- Python 3.8+
- Dependencies (already included in project):
  - `web3>=6.0.0` (with AsyncWeb3 and persistent WebSocket support)
  - `rich>=14.1.0`
  - `toml>=0.10.2`
  - `eth-abi>=5.2.0`

**Note**: This implementation uses `AsyncWeb3` with `WebSocketProvider` from `web3.providers.persistent`, following Monad's recommended patterns for WebSocket connections.

## Installation

The event listener is part of the staking-cli project. Make sure you've installed the project dependencies:

```bash
cd staking-sdk-cli
source cli-venv/bin/activate  # Activate virtual environment
pip install .
```

## Usage

### Using Config File

If you have a `config.toml` file with an RPC URL configured:

```bash
cd staking-cli
python event_listener.py --config-path ~/config.toml
```

The script will automatically convert HTTP(S) URLs to WebSocket URLs:
- `https://rpc.example.com` → `wss://rpc.example.com`
- `http://localhost:8545` → `ws://localhost:8545`

### Using Direct WebSocket URL

You can also specify the WebSocket URL directly:

```bash
python event_listener.py --ws-url wss://your-monad-node.com
```

### Command Line Options

```
--ws-url          WebSocket URL of the Monad node (e.g., wss://node.example.com)
--config-path     Path to config.toml file (default: ./config.toml)
```

### Speculative Execution (Advanced)

The event listener currently uses the standard `logs` subscription which returns finalized events. Monad also supports speculative execution via the `monadLogs` subscription type, which provides events ~1 second faster but before finalization.

To use speculative execution, you would modify line 297 in `event_listener.py`:

```python
# Standard (current implementation - finalized events)
subscription_id = await w3.eth.subscribe('logs', filter_params)

# Speculative (faster but may see competing blocks)
subscription_id = await w3.eth.subscribe('monadLogs', filter_params)
```

See the [Monad WebSocket Guide](../websockets.md) for more details on speculative execution and block commit states.

## Output Format

Each event is displayed in a color-coded panel with relevant information:

### Example: Delegate Event
```
┌─────────────────── Delegate ───────────────────┐
│ Block: 1234567                                 │
│ Time: 2025-10-15 14:32:10                     │
│ Tx Hash: 0xabcd1234...                        │
│                                                │
│ Validator ID: 42                               │
│ Delegator: 0x742d35Cc6634C0532925a3b844Bc9e7│
│ Amount: 1000.000000 MON                        │
│ Activation Epoch: 156                          │
└────────────────────────────────────────────────┘
Total events received: 1
```

### Example: ValidatorRewarded Event
```
┌──────────────── ValidatorRewarded ─────────────┐
│ Block: 1234568                                 │
│ Time: 2025-10-15 14:32:50                     │
│ Tx Hash: 0xef567890...                        │
│                                                │
│ Validator ID: 42                               │
│ From: 0x0000000000000000000000000000000000000│
│ Reward Amount: 10.000000 MON                   │
│ Epoch: 156                                     │
└────────────────────────────────────────────────┘
Total events received: 2
```

### Color Coding

- **Green** - ValidatorRewarded, ClaimRewards
- **Blue** - ValidatorCreated
- **Yellow** - ValidatorStatusChanged, CommissionChanged
- **Cyan** - Delegate
- **Magenta** - Undelegate
- **Red** - Withdraw
- **Bright Blue** - EpochChanged

## Features

- **Real-time monitoring** - Events appear immediately as they occur on-chain
- **Human-readable output** - All values are decoded and formatted (amounts in MON, percentages, etc.)
- **Color-coded display** - Easy visual distinction between event types
- **Connection status** - Clear feedback on connection status and errors
- **Event counter** - Track total number of events received
- **Graceful shutdown** - Press Ctrl+C to cleanly stop the listener

## Troubleshooting

### Connection Issues

**Problem**: `Failed to connect to WebSocket endpoint`

**Solutions**:
1. Verify your WebSocket URL is correct and accessible
2. Check if the node supports WebSocket connections
3. Ensure your firewall allows WebSocket connections
4. Try using `ws://` instead of `wss://` for local nodes

### Config File Issues

**Problem**: `Config file not found`

**Solutions**:
1. Verify the path to your `config.toml` file
2. Use absolute path: `--config-path /full/path/to/config.toml`
3. Or provide WebSocket URL directly with `--ws-url`

### No Events Appearing

**Possible causes**:
1. No staking activity on the network at the moment (normal)
2. Connection to node is established but no events are being emitted
3. Network is experiencing issues

The listener will continue running and display events as they occur.

## Architecture

### Implementation Details

The event listener follows Monad's WebSocket best practices using:
- **AsyncWeb3** with persistent **WebSocketProvider** (from `web3.providers.persistent`)
- **`eth_subscribe`** with `'logs'` subscription type and address filter
- **`w3.socket.process_subscriptions()`** async iterator for processing events

This implementation is based on the [Monad WebSocket Guide](../websockets.md) and uses the recommended async patterns for real-time data.

### Event Decoding Process

1. **Connect** - Establish WebSocket connection using `async with AsyncWeb3(WebSocketProvider(url))`
2. **Subscribe to logs** - Use `await w3.eth.subscribe('logs', filter_params)` with staking contract address filter
3. **Process subscriptions** - Iterate through `w3.socket.process_subscriptions()` for incoming events
4. **Extract payload** - Get log data from `payload['result']`
5. **Identify event type** - Match topic[0] (event signature) to event name
6. **Decode parameters**:
   - Indexed parameters (topics[1], topics[2], etc.) - decoded from topics
   - Non-indexed parameters - decoded from log data using ABI
7. **Format output** - Convert to human-readable format with proper units
8. **Display** - Render as color-coded panel in terminal

### Event Signatures

Event signatures are computed as `keccak256(eventSignature)`:

```python
# Example
"Delegate(uint64,address,uint256,uint64)" → 0x1234abcd...
```

The first topic (topics[0]) always contains the event signature hash, which is used to identify the event type.

## Examples

### Monitor All Staking Events

```bash
python event_listener.py --ws-url wss://testnet.monad.xyz
```

### Use with Local Node

```bash
python event_listener.py --ws-url ws://localhost:8545
```

### Use with Config File

```bash
# Ensure config.toml has rpc_url configured
python event_listener.py --config-path ./config.toml
```

## Integration with Staking CLI

The event listener is designed to work alongside the staking CLI. You can:

1. Run the event listener in one terminal
2. Execute staking operations in another terminal
3. See your transactions' events appear in real-time in the listener

Example workflow:
```bash
# Terminal 1: Start event listener
python event_listener.py --config-path ~/config.toml

# Terminal 2: Delegate to a validator
python main.py delegate --validator-id 42 --amount 1000 --config-path ~/config.toml

# Terminal 1 will show the Delegate event appear immediately
```

## Notes

- The listener maintains a history of the last 100 events in memory
- All amounts are automatically converted from wei to MON for readability
- Commission values are displayed as percentages
- Event timestamps are in local time
- Transaction hashes are truncated in display but full hash is available in the data

## Support

For issues or questions about the event listener:
1. Check this README's troubleshooting section
2. Verify your WebSocket connection
3. Check the main staking-cli README for general setup issues

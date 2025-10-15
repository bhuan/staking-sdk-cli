# Event Listener Quick Start

## 1. Activate Virtual Environment

```bash
cd staking-sdk-cli
source cli-venv/bin/activate
```

## 2. Run the Event Listener

### Option A: Using existing config.toml

```bash
cd staking-cli
python event_listener.py --config-path ~/config.toml
```

### Option B: Using direct WebSocket URL

```bash
cd staking-cli
python event_listener.py --ws-url wss://testnet-rpc.monad.xyz
```

## 3. Test with a Transaction

In another terminal, perform a staking operation:

```bash
source cli-venv/bin/activate
cd staking-cli
python main.py delegate --validator-id 1 --amount 100 --config-path ~/config.toml
```

You should see the Delegate event appear in the event listener terminal!

## Implementation Notes

The event listener is built using:

1. **AsyncWeb3** with **WebSocketProvider** from `web3.providers.persistent`
2. **`eth_subscribe('logs', filter_params)`** to subscribe to staking contract events
3. **`w3.socket.process_subscriptions()`** async iterator for real-time processing

This follows the pattern from Monad's WebSocket guide:

```python
async with AsyncWeb3(WebSocketProvider(ws_url)) as w3:
    subscription_id = await w3.eth.subscribe('logs', {'address': STAKING_CONTRACT})
    async for payload in w3.socket.process_subscriptions():
        log = payload['result']
        # Process log...
```

## Monitored Events

All 9 staking precompile events:

- ValidatorRewarded (green)
- ValidatorCreated (blue)
- ValidatorStatusChanged (yellow)
- Delegate (cyan)
- Undelegate (magenta)
- Withdraw (red)
- ClaimRewards (green)
- CommissionChanged (yellow)
- EpochChanged (bright_blue)

## Advanced: Custom Event Processing

See `example_event_listener.py` for how to use the `StakingEventListener` class programmatically with custom processing logic.

## Troubleshooting

### Connection Errors

If you get connection errors, verify:
1. WebSocket URL is correct and accessible
2. Node supports WebSocket connections
3. For `wss://` URLs, SSL/TLS is properly configured

### No Events Appearing

This is normal if there's no staking activity on the network. The listener will show events as they occur.

### Import Errors

Make sure you're in the virtual environment:
```bash
source cli-venv/bin/activate
pip install .  # Re-install if needed
```

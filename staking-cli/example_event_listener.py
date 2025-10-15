#!/usr/bin/env python3
"""
Example: Using the Staking Event Listener Programmatically

This example shows how to use the StakingEventListener class
in your own Python scripts for custom event processing.
"""

import asyncio
from rich.console import Console
from event_listener import StakingEventListener

async def main():
    """
    Example of using the event listener with custom processing.
    """
    console = Console()

    # Configure your WebSocket URL
    ws_url = "wss://testnet-rpc.monad.xyz"  # Replace with your node URL

    # Create the listener
    listener = StakingEventListener(ws_url, console)

    # You can access the events_history list to see past events
    # or modify the listener to add custom callbacks

    console.print("[bold cyan]Starting custom event listener...[/bold cyan]")
    console.print("[yellow]This example will print a summary every 10 events[/yellow]\n")

    try:
        await listener.listen_for_events()
    except KeyboardInterrupt:
        console.print("\n[yellow]Listener stopped[/yellow]")

        # Print summary
        if listener.events_history:
            console.print(f"\n[bold]Summary: Received {listener.event_count} total events[/bold]")

            # Count events by type
            event_counts = {}
            for event in listener.events_history:
                event_type = event['name']
                event_counts[event_type] = event_counts.get(event_type, 0) + 1

            console.print("\n[bold]Event breakdown:[/bold]")
            for event_type, count in sorted(event_counts.items()):
                console.print(f"  {event_type}: {count}")


if __name__ == "__main__":
    asyncio.run(main())

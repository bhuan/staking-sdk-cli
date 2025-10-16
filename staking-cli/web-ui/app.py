#!/usr/bin/env python3
"""
Monad Staking Events Web UI
Simple web interface to browse staking events from PostgreSQL
"""

from flask import Flask, render_template, jsonify, request
import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import logging
from decimal import Decimal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

# Database configuration from environment
DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ.get('DB_NAME', 'monad_events')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('DB_PASSWORD', '')

# Initialize connection pool
db_pool = None

def init_db_pool():
    """Initialize database connection pool"""
    global db_pool
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        logging.info("Database connection pool initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize database pool: {e}")
        raise

def get_db_connection():
    """Get database connection from pool"""
    if db_pool is None:
        init_db_pool()
    try:
        return db_pool.getconn()
    except Exception as e:
        logging.error(f"Failed to get connection from pool: {e}")
        raise

def return_db_connection(conn):
    """Return connection to pool"""
    if db_pool and conn:
        db_pool.putconn(conn)

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/api/stats')
def api_stats():
    """Get dashboard statistics"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Total events
        cur.execute('SELECT COUNT(*) as total FROM staking_events')
        total_events = cur.fetchone()['total']

        # Recent activity (last 24h)
        cur.execute('''
            SELECT COUNT(*) as count FROM staking_events
            WHERE timestamp > NOW() - INTERVAL '24 hours'
        ''')
        recent_count = cur.fetchone()['count']

        cur.close()

        return jsonify({
            'total_events': total_events,
            'recent_24h': recent_count
        })
    except Exception as e:
        logging.error(f"Error in api_stats: {e}")
        return jsonify({'error': 'Database error'}), 500
    finally:
        if conn:
            return_db_connection(conn)

@app.route('/api/events')
def api_events():
    """Get paginated events with filters"""
    conn = None
    try:
        # Pagination - validate inputs
        try:
            page = max(1, int(request.args.get('page', 1)))
            per_page = min(100, max(1, int(request.args.get('per_page', 50))))  # Cap at 100
        except ValueError:
            return jsonify({'error': 'Invalid pagination parameters'}), 400

        offset = (page - 1) * per_page

        # Filters
        event_type = request.args.get('event_type')
        validator_id = request.args.get('validator_id')
        address = request.args.get('address')
        hours_str = request.args.get('hours', '24')

        # Validate hours parameter
        try:
            hours = int(hours_str)
            if hours < 0 or hours > 8760:  # Max 1 year
                return jsonify({'error': 'Invalid hours parameter (must be 0-8760)'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid hours parameter'}), 400

        # Build query with parameterized values
        where_clauses = []
        params = []

        if hours > 0:
            where_clauses.append("timestamp > NOW() - INTERVAL '%s hours'")
            params.append(hours)

        if event_type:
            where_clauses.append('event_name = %s')
            params.append(event_type)

        if validator_id:
            try:
                where_clauses.append('validator_id = %s')
                params.append(int(validator_id))
            except ValueError:
                return jsonify({'error': 'Invalid validator_id'}), 400

        if address:
            where_clauses.append('LOWER(address) = %s')
            params.append(address.lower())

        where_sql = 'WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get total count
        cur.execute(f'SELECT COUNT(*) as total FROM staking_events {where_sql}', params)
        total = cur.fetchone()['total']

        # Get events
        params_with_limit = params + [per_page, offset]
        cur.execute(f'''
            SELECT
                id,
                timestamp,
                event_name,
                block_number,
                transaction_hash,
                validator_id,
                address,
                amount,
                epoch,
                event_data
            FROM staking_events
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
        ''', params_with_limit)

        events = cur.fetchall()

        cur.close()

        # Convert to dict and handle Decimals
        events_list = []
        for event in events:
            event_dict = dict(event)
            if event_dict['amount']:
                # Convert from wei to MON
                event_dict['amount'] = float(event_dict['amount']) / 1e18
            event_dict['timestamp'] = event_dict['timestamp'].isoformat()
            events_list.append(event_dict)

        return jsonify({
            'events': events_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        logging.error(f"Error in api_events: {e}")
        return jsonify({'error': 'Database error'}), 500
    finally:
        if conn:
            return_db_connection(conn)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

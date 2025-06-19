
import threading
import time
from flask import Flask, jsonify
import requests
import os

app = Flask('keep_alive')

@app.route('/')
def keep_alive():
    return jsonify({
        "status": "alive",
        "message": "Telegram Reaction Bot System is running",
        "timestamp": time.time()
    })

@app.route('/status')
def status():
    return jsonify({
        "system": "Telegram Reaction Bot System",
        "active": True,
        "uptime": time.time()
    })

def run_keep_alive():
    """Run the keep alive server"""
    app.run(host='0.0.0.0', port=8080, debug=False)

def start_keep_alive():
    """Start keep alive in a separate thread"""
    server_thread = threading.Thread(target=run_keep_alive, daemon=True)
    server_thread.start()
    print("Keep alive server started on port 8080")

# Auto-ping function to keep the service active
def auto_ping():
    """Ping the service periodically to keep it alive"""
    while True:
        try:
            time.sleep(300)  # Wait 5 minutes
            # You can add auto-ping logic here if needed
        except Exception as e:
            print(f"Auto-ping error: {e}")

if __name__ == "__main__":
    start_keep_alive()
    # Start auto-ping in background
    ping_thread = threading.Thread(target=auto_ping, daemon=True)
    ping_thread.start()

    # Keep the main thread alive
    while True:
        time.sleep(1)

#!/usr/bin/env python3
"""Check why scores aren't showing up for a hotkey.

This script queries the database to diagnose score calculation issues.
"""

import sys
import os

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass

from database import connect, get_cursor, close_connection, get_block_window_start, BLOCKS_PER_WINDOW, init_connection_pool
from block_utils import get_current_block


def check_hotkey_scores(miner_hotkey: str):
    """Check scores for a specific hotkey"""
    # Initialize pool if not already initialized
    init_connection_pool()
    
    conn = None
    try:
        conn = connect()
        c = get_cursor(conn)
        
        current_block = get_current_block()
        window_start = get_block_window_start(current_block)
        window_end = window_start + BLOCKS_PER_WINDOW - 1
        
        print(f"Current block: {current_block}")
        print(f"Current window: {window_start} - {window_end}")
        print(f"Blocks per window: {BLOCKS_PER_WINDOW}")
        print()
        
        # Check all submissions for this hotkey
        print(f"=== All submissions for {miner_hotkey} ===")
        c.execute("""
            SELECT post_id, accepted_block, accepted_at, score, 
                   selected_for_validation, validation_id, 
                   x_validated, x_validation_result
            FROM submissions
            WHERE miner_hotkey = %s
            ORDER BY accepted_block DESC
            LIMIT 20
        """, (miner_hotkey,))
        
        submissions = c.fetchall()
        if not submissions:
            print("  No submissions found!")
            return
        
        print(f"  Found {len(submissions)} submission(s):")
        for sub in submissions:
            sub_window_start = get_block_window_start(sub["accepted_block"])
            in_current_window = window_start <= sub["accepted_block"] <= window_end
            print(f"  - post_id={sub['post_id']}")
            print(f"    accepted_block={sub['accepted_block']} (window: {sub_window_start}-{sub_window_start + BLOCKS_PER_WINDOW - 1})")
            print(f"    in_current_window={in_current_window}")
            print(f"    score={sub['score']}")
            print(f"    selected_for_validation={sub['selected_for_validation']}")
            print(f"    validation_id={sub['validation_id']}")
            print(f"    x_validated={sub['x_validated']}, x_validation_result={sub['x_validation_result']}")
            print()
        
        # Check submissions in current window
        print(f"=== Submissions in current window ({window_start}-{window_end}) ===")
        c.execute("""
            SELECT post_id, accepted_block, score, COUNT(*) as count
            FROM submissions
            WHERE miner_hotkey = %s 
              AND accepted_block >= %s 
              AND accepted_block <= %s
            GROUP BY post_id, accepted_block, score
        """, (miner_hotkey, window_start, current_block))
        
        current_window_subs = c.fetchall()
        if not current_window_subs:
            print("  No submissions in current window!")
        else:
            print(f"  Found {len(current_window_subs)} submission(s) in current window:")
            for sub in current_window_subs:
                print(f"  - post_id={sub['post_id']}, block={sub['accepted_block']}, score={sub['score']}")
            
            # Calculate average score
            c.execute("""
                SELECT AVG(score) as avg_score, COUNT(*) as count
                FROM submissions
                WHERE miner_hotkey = %s 
                  AND accepted_block >= %s 
                  AND accepted_block <= %s
            """, (miner_hotkey, window_start, current_block))
            
            avg_row = c.fetchone()
            if avg_row and avg_row["count"] > 0:
                print(f"  Average score: {avg_row['avg_score']:.6f} ({avg_row['count']} submissions)")
        
        print()
        
        # Check validation results
        print(f"=== Validation results for {miner_hotkey} ===")
        c.execute("""
            SELECT vr.validation_id, vr.success, vr.validated_at,
                   s.accepted_block, s.post_id
            FROM validation_results vr
            INNER JOIN submissions s ON vr.validation_id = s.validation_id
            WHERE vr.miner_hotkey = %s
            ORDER BY vr.validated_at DESC
            LIMIT 10
        """, (miner_hotkey,))
        
        validations = c.fetchall()
        if not validations:
            print("  No validation results found!")
        else:
            print(f"  Found {len(validations)} validation result(s):")
            for val in validations:
                val_window_start = get_block_window_start(val["accepted_block"])
                in_current_window = window_start <= val["accepted_block"] <= window_end
                print(f"  - validation_id={val['validation_id']}")
                print(f"    success={val['success']}")
                print(f"    post_id={val['post_id']}")
                print(f"    accepted_block={val['accepted_block']} (window: {val_window_start}-{val_window_start + BLOCKS_PER_WINDOW - 1})")
                print(f"    in_current_window={in_current_window}")
                print(f"    validated_at={val['validated_at']}")
                print()
        
        # Check failed validations in current window
        print(f"=== Failed validations in current window ===")
        c.execute("""
            SELECT DISTINCT vr.miner_hotkey
            FROM validation_results vr
            INNER JOIN submissions s ON vr.validation_id = s.validation_id
            WHERE s.accepted_block >= %s 
              AND s.accepted_block <= %s
              AND vr.success = 0
              AND vr.miner_hotkey = %s
        """, (window_start, current_block, miner_hotkey))
        
        failed = c.fetchall()
        if failed:
            print(f"  ⚠️  Found {len(failed)} failed validation(s) in current window!")
            print("  This would set the score to 0.0")
        else:
            print("  ✓ No failed validations in current window")
        
        print()
        
        # Simulate the scores query
        print(f"=== Simulated scores query result ===")
        c.execute("""
            SELECT miner_hotkey, AVG(score) as avg_score, COUNT(*) as count
            FROM submissions
            WHERE miner_hotkey = %s 
              AND accepted_block >= %s 
              AND accepted_block <= %s
            GROUP BY miner_hotkey
        """, (miner_hotkey, window_start, current_block))
        
        score_row = c.fetchone()
        if score_row and score_row["count"] > 0:
            print(f"  Would return: score={score_row['avg_score']:.6f} ({score_row['count']} submissions)")
        else:
            print("  Would return: NOT IN RESULTS (no submissions in current window)")
        
    finally:
        if conn:
            close_connection(conn)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_scores.py <miner_hotkey>")
        print("Example: python check_scores.py 5GUDZqzmvTQ1UiTUoFBS96USxsSjF6GQnnL4pnNhf7AmWL7c")
        sys.exit(1)
    
    miner_hotkey = sys.argv[1]
    check_hotkey_scores(miner_hotkey)


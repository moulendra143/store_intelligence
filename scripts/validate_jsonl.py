import json
import sys

def validate_jsonl(filepath):
    print(f"Validating {filepath}...")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading file: {e}")
        return False

    valid_count = 0
    errors = 0
    for idx, line in enumerate(lines, 1):
        line_str = line.strip()
        if not line_str:
            continue
        try:
            data = json.loads(line_str)
            valid_count += 1
            # Perform a basic check to ensure expected fields exist
            # For the sample schema (e.g. event_type, store_id/store_code, camera_id)
            if 'event_type' not in data:
                print(f"  Line {idx}: Missing 'event_type'")
                errors += 1
        except json.JSONDecodeError as je:
            print(f"  Line {idx}: Invalid JSON: {je}")
            errors += 1

    print(f"Summary for {filepath}: {valid_count} valid JSON lines, {errors} errors found.")
    return errors == 0

if __name__ == '__main__':
    v1 = validate_jsonl('data/sample_eventsbe42122.jsonl')
    v2 = validate_jsonl('data/events.jsonl')
    if not (v1 and v2):
        sys.exit(1)
    else:
        print("All checks passed successfully.")

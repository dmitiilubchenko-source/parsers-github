"""python assignment.py [--retry-missing]: export coursework party CSV."""
import argparse
import json
from election_parser.assignment import export

if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Партийные протоколы: строки 1–22, одна запись на УИК')
    p.add_argument('--retry-missing', action='store_true', help='Сначала дозагрузить отсутствующие партийные протоколы')
    p.add_argument('--workers', type=int, choices=range(1, 9), default=4)
    args = p.parse_args()
    result = export(retry=args.retry_missing, workers=args.workers)
    if result.get('retry') and 'errors' in result['retry']:
        result['retry'] = {k: v for k, v in result['retry'].items() if k != 'errors'} | {'errors': len(result['retry']['errors'])}
    print(json.dumps(result, ensure_ascii=False, indent=2))

"""Check the actual rendered Kubernetes image against the Docker release pin."""
from pathlib import Path
import sys
import yaml

root = Path(__file__).resolve().parents[1]
compose = yaml.safe_load((root / 'compose.yaml').read_text())
expected = compose['services']['devbox']['image']
deployment = next(item for item in yaml.safe_load_all(sys.stdin) if item['kind'] == 'Deployment')
actual = deployment['spec']['template']['spec']['containers'][0]['image']
if actual != expected:
    raise SystemExit(f'Rendered Kubernetes image {actual} differs from Docker image {expected}')
print('Rendered Kubernetes release image matches Docker:', actual)

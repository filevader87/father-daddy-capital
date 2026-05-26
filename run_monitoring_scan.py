import json
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()
print(json.dumps(result, indent=2, default=str))
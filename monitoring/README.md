# Monitoring

Monitoring is currently local-process only. Docker-based monitoring files were removed.

## Current Components

- `monitoring/exporters/metrics_exporter.py`: Python metrics exporter.
- `config/monitoring/prometheus.yml`: Prometheus scrape configuration.
- `config/monitoring/rules/alerts.yml`: alert rules.
- `config/monitoring/dashboards/system_overview.json`: dashboard definition.

## Run Exporter

```powershell
python monitoring/exporters/metrics_exporter.py
```

Prometheus and Grafana should be installed and managed outside this repository if needed. Do not add Docker Compose back as an application dependency.

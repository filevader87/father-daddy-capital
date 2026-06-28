import logging, sys
import json_log_formatter

formatter = json_log_formatter.JSONFormatter()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)
logger = logging.getLogger("fdc")
logger.addHandler(handler)
logger.setLevel(logging.INFO) 
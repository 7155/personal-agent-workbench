"""Use the user's existing Earth Engine authorization; stdout is a private pipe."""
import sys

import ee
from google.auth.transport.requests import Request

try:
    credentials = ee.data.get_persistent_credentials()
    credentials.refresh(Request())
    sys.stdout.write(credentials.token)
except Exception:
    sys.stderr.write("Earth Engine authorization unavailable. Run earthengine authenticate in this Python environment.\n")
    sys.exit(1)

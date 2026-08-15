from app.core.plugins.servarr import ServarrPlugin


class LidarrPlugin(ServarrPlugin):
    app_name = "Lidarr"
    api_prefix = "/api/v1"
    database_members = ("lidarr.db",)

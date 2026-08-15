from app.core.plugins.servarr import ServarrPlugin


class RadarrPlugin(ServarrPlugin):
    app_name = "Radarr"
    api_prefix = "/api/v3"
    database_members = ("radarr.db", "nzbdrone.db")

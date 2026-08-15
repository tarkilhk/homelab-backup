from app.core.plugins.servarr import ServarrPlugin


class SonarrPlugin(ServarrPlugin):
    app_name = "Sonarr"
    api_prefix = "/api/v3"
    database_members = ("sonarr.db", "nzbdrone.db")

"""The server-rendered pages, one router per area. None of them appear in the OpenAPI document,
which describes the JSON API only."""

from app.web.pages import account, onboarding, organisations, projects, suggestions

routers = (
    account.router,
    onboarding.router,
    organisations.router,
    projects.router,
    suggestions.router,
)

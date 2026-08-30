"""HTTP routes for the review UI, one module per screen.

Every module here exports a bare `router` that `web.app.create_app` includes.
Routers reach their dependencies through `request.app.state` (`settings`, `store`,
`jobs`, `templates`) rather than importing an application singleton, because
`create_app` is a factory: two apps in one process must not share a store.

`web.media` predates this package and keeps its router at the top level. It is
left where it is deliberately — it is a security-critical module that other code
imports by path (`videomaker.web.media.safe_project_path`), and moving it would
churn those imports for no behavioural gain.
"""

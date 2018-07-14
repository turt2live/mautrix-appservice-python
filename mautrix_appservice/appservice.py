# -*- coding: future_fstrings -*-
# Partly based on github.com/Cadair/python-appservice-framework (MIT license)
from contextlib import contextmanager
from typing import Optional, Callable, Awaitable, Union
from aiohttp import web
import aiohttp
import asyncio
import logging

from .intent_api import HTTPAPI, IntentAPI
from .state_store import StateStore, JSONStateStore

QueryFunc = Callable[[web.Request], Awaitable[Optional[web.Response]]]
HandlerFunc = Callable[[dict], Awaitable]


class AppService:
    def __init__(self, server: str, domain: str, as_token: str, hs_token: str, bot_localpart: str,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 log: Optional[Union[logging.Logger, str]] = None, verify_ssl: bool = True,
                 query_user: QueryFunc = None, query_alias: QueryFunc = None,
                 state_store: StateStore = None):
        self.server = server
        self.domain = domain
        self.verify_ssl = verify_ssl
        self.as_token = as_token
        self.hs_token = hs_token
        self.bot_mxid = f"@{bot_localpart}:{domain}"
        if isinstance(state_store, StateStore):
            self.state_store = state_store
        else:
            file = state_store if isinstance(state_store, str) else "mx-state.json"
            self.state_store = JSONStateStore(autosave_file=file)
            self.state_store.load(file)

        self.transactions = []

        self._http_session = None
        self._intent = None

        self.loop = loop or asyncio.get_event_loop()
        self.log = (logging.getLogger(log) if isinstance(log, str)
                    else log or logging.getLogger("mautrix_appservice"))

        async def default_query_handler(_):
            return None

        self.query_user = query_user or default_query_handler
        self.query_alias = query_alias or default_query_handler

        self.event_handlers = []

        self.app = web.Application(loop=self.loop, client_max_size=1024**3)
        self.app.router.add_route("PUT", "/transactions/{transaction_id}",
                                  self._http_handle_transaction)
        self.app.router.add_route("GET", "/rooms/{alias}", self._http_query_alias)
        self.app.router.add_route("GET", "/users/{user_id}", self._http_query_user)

        async def update_state(event):
            self.state_store.update_state(event)

        self.matrix_event_handler(update_state)

    @property
    def http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None:
            raise AttributeError("the http_session attribute can only be used "
                                 "from within the `AppService.run` context manager")
        else:
            return self._http_session

    @property
    def intent(self) -> IntentAPI:
        if self._intent is None:
            raise AttributeError("the intent attribute can only be used from "
                                 "within the `AppService.run` context manager")
        else:
            return self._intent

    @contextmanager
    def run(self, host: str = "127.0.0.1", port: int = 8080):
        connector = None
        if self.server.startswith("https://") and not self.verify_ssl:
            connector = aiohttp.TCPConnector(verify_ssl=False)
        self._http_session = aiohttp.ClientSession(loop=self.loop, connector=connector)
        self._intent = HTTPAPI(base_url=self.server, domain=self.domain, bot_mxid=self.bot_mxid,
                               token=self.as_token, log=self.log, state_store=self.state_store,
                               client_session=self._http_session).bot_intent()

        yield self.loop.create_server(self.app.make_handler(), host, port)

        self._intent = None
        self._http_session.close()
        self._http_session = None

    def _check_token(self, request: web.Request) -> bool:
        try:
            token = request.rel_url.query["access_token"]
        except KeyError:
            return False

        if token != self.hs_token:
            return False

        return True

    async def _http_query_user(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return web.Response(status=401)

        try:
            user_id = request.match_info["userId"]
        except KeyError:
            return web.Response(status=400)

        try:
            response = await self.query_user(user_id)
        except Exception:
            self.log.exception("Exception in user query handler")
            return web.Response(status=500)

        if not response:
            return web.Response(status=404)
        return web.json_response(response)

    async def _http_query_alias(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return web.Response(status=401)

        try:
            alias = request.match_info["alias"]
        except KeyError:
            return web.Response(status=400)

        try:
            response = await self.query_alias(alias)
        except Exception:
            self.log.exception("Exception in alias query handler")
            return web.Response(status=500)

        if not response:
            return web.Response(status=404)
        return web.json_response(response)

    async def _http_handle_transaction(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return web.Response(status=401)

        transaction_id = request.match_info["transaction_id"]
        if transaction_id in self.transactions:
            return web.json_response({})  # 200 OK

        json = await request.json()

        try:
            events = json["events"]
        except KeyError:
            return web.Response(status=400)

        for event in events:
            self.handle_matrix_event(event)

        self.transactions.append(transaction_id)

        return web.json_response({})

    def handle_matrix_event(self, event: dict):
        async def try_handle(handler_func: HandlerFunc):
            try:
                await handler_func(event)
            except Exception:
                self.log.exception("Exception in Matrix event handler")

        for handler in self.event_handlers:
            asyncio.ensure_future(try_handle(handler), loop=self.loop)

    def matrix_event_handler(self, func: HandlerFunc) -> HandlerFunc:
        self.event_handlers.append(func)
        return func

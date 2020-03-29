import asyncio
import logging
import socket
from random import random
from typing import AsyncGenerator
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import orjson
from aiohttp import ClientSession
from binance.client import AsyncClient
from binance import enums
from websockets.client import WebSocketClientProtocol
from websockets.client import connect
from websockets.exceptions import ConnectionClosed

_logger = logging.getLogger()


class ReconnectingWebsocket:

    STREAM_URL = "wss://stream.binance.com:9443/"
    MAX_RECONNECTS = 20
    MAX_RECONNECT_SECONDS = 60
    MIN_RECONNECT_WAIT = 0.1
    TIMEOUT = 2

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        path: str,
        coro: Callable,
        prefix: str = "ws/",
    ) -> None:
        self._loop = loop
        self._log = logging.getLogger(__name__)
        self._path = path
        self._coro = coro
        self._prefix = prefix

        self._reconnects = 0
        self._conn: Optional[asyncio.Future] = None
        self._socket: Optional[WebSocketClientProtocol] = None
        self._connect()

    def _connect(self) -> None:
        self._conn = asyncio.ensure_future(self._run(), loop=self._loop)

    async def _run(self) -> None:

        keep_waiting = True

        ws_url = self.STREAM_URL + self._prefix + self._path
        try:
            async with connect(
                ws_url, ping_interval=self.TIMEOUT, ping_timeout=self.TIMEOUT, timeout=self.TIMEOUT
            ) as _socket:
                _logger.warning(f"Connected to {self._path}")
                self._socket = _socket
                self._reconnects = 0
                while keep_waiting:
                    try:
                        evt = await asyncio.wait_for(
                            self._socket.recv(), timeout=self.TIMEOUT
                        )
                    except (asyncio.TimeoutError, ConnectionClosed):
                        try:
                            pong = await self._socket.ping()
                            await asyncio.wait_for(
                                pong, timeout=self._socket.ping_timeout
                            )
                            _logger.debug("Ping OK, keeping connection alive...")
                            continue
                        except Exception:
                            raise
                    # except asyncio.CancelledError:
                    #     self._log.debug("cancelled error")
                    #     raise asyncio.CancelledError
                    else:
                        try:
                            evt_obj = orjson.loads(evt)
                        except (orjson.JSONDecodeError, orjson.JSONEncodeError):
                            self._log.error(f"error parsing evt json:{str(evt)}")
                        else:
                            await self._coro(evt_obj)

        except socket.gaierror as e:
            self._log.debug(e)
            await self._reconnect()

        except asyncio.CancelledError:
            self._log.debug("cancelled error")
            await self._reconnect()

        except ConnectionClosed as e:
            self._log.debug("ws connection closed:{}".format(e))
            await self._reconnect()
        except Exception as e:
            self._log.debug("ws exception:{}".format(e))
            await self._reconnect()

    def _get_reconnect_wait(self, attempts: int) -> int:
        expo = 2 ** attempts
        return round(random() * min(self.MAX_RECONNECT_SECONDS, expo - 1) + 1)

    async def _reconnect(self) -> None:
        await self.cancel()
        self._reconnects += 1
        if self._reconnects < self.MAX_RECONNECTS:
            reconnect_wait = self._get_reconnect_wait(self._reconnects)

            self._log.info(
                f"websocket {self._path} reconnecting "
                f"{self.MAX_RECONNECTS - self._reconnects} "
                f"reconnects left waiting {reconnect_wait}"
            )
            await asyncio.sleep(reconnect_wait)
            self._connect()
        else:
            self._log.error("Max reconnections {} reached:".format(self.MAX_RECONNECTS))

    async def send_ping(self) -> None:
        if self._socket:
            await self._socket.ping()

    async def cancel(self) -> None:
        self._log.warning(f"Cancelling {self._path}")
        if self._socket:
            self._log.debug("Closing socket")
            await self._socket.close()
            self._log.debug("Socket Closed")
        if self._conn:
            self._conn.cancel()
            try:
                self._log.debug("Cancelling Task")
                await self._conn
            except asyncio.CancelledError:
                pass
        self._log.debug("Task Cancelled")
        self._socket = None


class BinanceSocketManager:

    WEBSOCKET_DEPTH_5 = "5"
    WEBSOCKET_DEPTH_10 = "10"
    WEBSOCKET_DEPTH_20 = "20"

    _user_timeout = 30 * 60  # 30 minutes

    def __init__(self, client: AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        """Initialise the BinanceSocketManager

        :param client: Binance API client
        :type client: binance.Client

        """
        self._conns: Dict = {}
        self._user_timer = None
        self._user_listen_key = None
        self._user_callback = None
        self._client = client
        self._loop = loop
        self._log = logging.getLogger(__name__)

    async def _start_socket(
        self, path: str, coro: Callable, prefix: str = "ws/"
    ) -> Optional[str]:

        if path in self._conns:
            return None

        self._conns[path] = ReconnectingWebsocket(self._loop, path, coro, prefix)

        return path

    async def start_depth_socket(
        self, symbol: str, coro: Callable, depth: Optional[int] = None
    ) -> str:
        """Start a websocket for symbol market depth returning either a diff or a partial book

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#partial-book-depth-streams

        :param symbol: required
        :type symbol: str
        :param coro: callback coroutine to handle messages
        :type coro: async coroutine
        :param depth: optional Number of depth entries to return, default None. If passed returns a partial book instead of a diff
        :type depth: str

        :returns: connection key string if successful, False otherwise

        Partial Message Format

        .. code-block:: python

            {
                "lastUpdateId": 160,  # Last update ID
                "bids": [             # Bids to be updated
                    [
                        "0.0024",     # price level to be updated
                        "10",         # quantity
                        []            # ignore
                    ]
                ],
                "asks": [             # Asks to be updated
                    [
                        "0.0026",     # price level to be updated
                        "100",        # quantity
                        []            # ignore
                    ]
                ]
            }


        Diff Message Format

        .. code-block:: python

            {
                "e": "depthUpdate", # Event type
                "E": 123456789,     # Event time
                "s": "BNBBTC",      # Symbol
                "U": 157,           # First update ID in event
                "u": 160,           # Final update ID in event
                "b": [              # Bids to be updated
                    [
                        "0.0024",   # price level to be updated
                        "10",       # quantity
                        []          # ignore
                    ]
                ],
                "a": [              # Asks to be updated
                    [
                        "0.0026",   # price level to be updated
                        "100",      # quantity
                        []          # ignore
                    ]
                ]
            }

        """
        socket_name = symbol.lower() + "@depth"
        if depth and depth != "1":
            socket_name = "{}{}".format(socket_name, depth)
        await self._start_socket(socket_name, coro)
        return socket_name

    async def start_kline_socket(
        self, symbol: str, coro: Callable, interval: str = enums.KLINE_INTERVAL_1MINUTE
    ) -> str:
        """Start a websocket for symbol kline data

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#klinecandlestick-streams

        :param symbol: required
        :type symbol: str
        :param coro: callback function to handle messages
        :type coro: async coroutine
        :param interval: Kline interval, default KLINE_INTERVAL_1MINUTE
        :type interval: str

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            {
                "e": "kline",					# event type
                "E": 1499404907056,				# event time
                "s": "ETHBTC",					# symbol
                "k": {
                    "t": 1499404860000, 		# start time of this bar
                    "T": 1499404919999, 		# end time of this bar
                    "s": "ETHBTC",				# symbol
                    "i": "1m",					# interval
                    "f": 77462,					# first trade id
                    "L": 77465,					# last trade id
                    "o": "0.10278577",			# open
                    "c": "0.10278645",			# close
                    "h": "0.10278712",			# high
                    "l": "0.10278518",			# low
                    "v": "17.47929838",			# volume
                    "n": 4,						# number of trades
                    "x": false,					# whether this bar is final
                    "q": "1.79662878",			# quote volume
                    "V": "2.34879839",			# volume of active buy
                    "Q": "0.24142166",			# quote volume of active buy
                    "B": "13279784.01349473"	# can be ignored
                    }
            }
        """
        path = "{}@kline_{}".format(symbol.lower(), interval)
        await self._start_socket(path, coro)
        return path

    async def start_miniticker_socket(
        self, coro: Callable, update_time: Optional[int] = 1000
    ) -> str:
        """Start a miniticker websocket for all trades

        This is not in the official Binance api docs, but this is what
        feeds the right column on a ticker page on Binance.

        :param coro: callback function to handle messages
        :type coro: async coroutine
        :param update_time: time between callbacks in milliseconds, must be 1000 or greater
        :type update_time: int

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            [
                {
                    'e': '24hrMiniTicker',  # Event type
                    'E': 1515906156273,     # Event time
                    's': 'QTUMETH',         # Symbol
                    'c': '0.03836900',      # close
                    'o': '0.03953500',      # open
                    'h': '0.04400000',      # high
                    'l': '0.03756000',      # low
                    'v': '147435.80000000', # volume
                    'q': '5903.84338533'    # quote volume
                }
            ]
        """

        path = "!miniTicker@arr@{}ms".format(update_time)
        await self._start_socket("!miniTicker@arr@{}ms".format(update_time), coro)
        return path

    async def start_trade_socket(self, symbol: str, coro: Callable) -> str:
        """Start a websocket for symbol trade data

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#trade-streams

        :param symbol: required
        :type symbol: str
        :param coro: async coroutine function to handle messages
        :type coro: async function

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            {
                "e": "trade",     # Event type
                "E": 123456789,   # Event time
                "s": "BNBBTC",    # Symbol
                "t": 12345,       # Trade ID
                "p": "0.001",     # Price
                "q": "100",       # Quantity
                "b": 88,          # Buyer order Id
                "a": 50,          # Seller order Id
                "T": 123456785,   # Trade time
                "m": true,        # Is the buyer the market maker?
                "M": true         # Ignore.
            }

        """

        # this allows execution to keep going
        path = symbol.lower() + "@trade"
        await self._start_socket(path, coro)
        return path

    async def start_aggtrade_socket(self, symbol: str, coro: Callable) -> str:
        """Start a websocket for symbol trade data

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#aggregate-trade-streams

        :param symbol: required
        :type symbol: str
        :param coro: callback function to handle messages
        :type coro: function

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            {
                "e": "aggTrade",		# event type
                "E": 1499405254326,		# event time
                "s": "ETHBTC",			# symbol
                "a": 70232,				# aggregated tradeid
                "p": "0.10281118",		# price
                "q": "8.15632997",		# quantity
                "f": 77489,				# first breakdown trade id
                "l": 77489,				# last breakdown trade id
                "T": 1499405254324,		# trade time
                "m": false,				# whether buyer is a maker
                "M": true				# can be ignored
            }

        """
        path = symbol.lower() + "@aggTrade"
        await self._start_socket(symbol.lower() + "@aggTrade", coro)
        return path

    async def start_symbol_ticker_socket(self, symbol: str, coro: Callable) -> str:
        """Start a websocket for a symbol's ticker data

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#individual-symbol-ticker-streams

        :param symbol: required
        :type symbol: str
        :param coro: callback function to handle messages
        :type coro: function

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            {
                "e": "24hrTicker",  # Event type
                "E": 123456789,     # Event time
                "s": "BNBBTC",      # Symbol
                "p": "0.0015",      # Price change
                "P": "250.00",      # Price change percent
                "w": "0.0018",      # Weighted average price
                "x": "0.0009",      # Previous day's close price
                "c": "0.0025",      # Current day's close price
                "Q": "10",          # Close trade's quantity
                "b": "0.0024",      # Best bid price
                "B": "10",          # Bid bid quantity
                "a": "0.0026",      # Best ask price
                "A": "100",         # Best ask quantity
                "o": "0.0010",      # Open price
                "h": "0.0025",      # High price
                "l": "0.0010",      # Low price
                "v": "10000",       # Total traded base asset volume
                "q": "18",          # Total traded quote asset volume
                "O": 0,             # Statistics open time
                "C": 86400000,      # Statistics close time
                "F": 0,             # First trade ID
                "L": 18150,         # Last trade Id
                "n": 18151          # Total number of trades
            }

        """
        path = symbol.lower() + "@ticker"
        await self._start_socket(symbol.lower() + "@ticker", coro)
        return path

    async def start_ticker_socket(self, coro: Callable) -> str:
        """Start a websocket for all ticker data

        By default all markets are included in an array.

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md#all-market-tickers-stream

        :param coro: callback function to handle messages
        :type coro: function

        :returns: connection key string if successful, False otherwise

        Message Format

        .. code-block:: python

            [
                {
                    'F': 278610,
                    'o': '0.07393000',
                    's': 'BCCBTC',
                    'C': 1509622420916,
                    'b': '0.07800800',
                    'l': '0.07160300',
                    'h': '0.08199900',
                    'L': 287722,
                    'P': '6.694',
                    'Q': '0.10000000',
                    'q': '1202.67106335',
                    'p': '0.00494900',
                    'O': 1509536020916,
                    'a': '0.07887800',
                    'n': 9113,
                    'B': '1.00000000',
                    'c': '0.07887900',
                    'x': '0.07399600',
                    'w': '0.07639068',
                    'A': '2.41900000',
                    'v': '15743.68900000'
                }
            ]
        """
        path = "!ticker@arr"
        await self._start_socket(path, coro)
        return path

    async def start_multiplex_socket(self, streams: List[str], coro: Callable) -> str:
        """Start a multiplexed socket using a list of socket names.
        User stream sockets can not be included.

        Symbols in socket name must be lowercase i.e bnbbtc@aggTrade, neobtc@ticker

        Combined stream events are wrapped as follows: {"stream":"<streamName>","data":<rawPayload>}

        https://github.com/binance-exchange/binance-official-api-docs/blob/master/web-socket-streams.md

        :param streams: list of stream names in lower case
        :type streams: list
        :param coro: callback function to handle messages
        :type coro: async function

        :returns: connection key string if successful, False otherwise

        Message Format - see Binance API docs for all types

        """
        path = "streams={}".format("/".join(streams))
        await self._start_socket(path, coro, "stream?")
        return path

    async def start_user_socket(self, coro: Callable) -> str:
        """Start a websocket for user data

        https://www.binance.com/restapipub.html#user-wss-endpoint

        :param coro: callback function to handle messages
        :type coro: function

        :returns: connection key string if successful, False otherwise

        Message Format - see Binance API docs for all types
        """
        # Get the user listen key
        user_listen_key = await self._client.stream_get_listen_key()
        # and start the socket with this specific key
        conn_key = await self._start_user_socket(user_listen_key, coro)
        return conn_key

    async def _start_user_socket(self, user_listen_key: str, callback: Callable) -> str:
        # With this function we can start a user socket with a specific key
        if self._user_listen_key:
            # cleanup any sockets with this key
            for conn_key in self._conns:
                if len(conn_key) >= 60 and conn_key[:60] == self._user_listen_key:
                    await self.stop_socket(conn_key)
                    break
        self._user_listen_key = user_listen_key
        self._user_callback = callback
        conn_key = await self._start_socket(self._user_listen_key, callback)
        if conn_key:
            # start timer to keep socket alive
            self._start_user_timer()

        return conn_key

    def _start_user_timer(self) -> None:
        self._user_timer = self._loop.call_later(
            self._user_timeout, self._keepalive_user_socket
        )

    def _keepalive_user_socket(self) -> None:
        async def _run():
            user_listen_key = await self._client.stream_get_listen_key()
            self._log.debug(
                "new key {} old key {}".format(user_listen_key, self._user_listen_key)
            )
            # check if they key changed and reconnect
            if user_listen_key != self._user_listen_key:
                # Start a new socket with the key received
                # `_start_user_socket` automatically cleanup open sockets
                # and starts timer to keep socket alive
                await self._start_user_socket(user_listen_key, self._user_callback)
            else:
                # Restart timer only if the user listen key is not changed
                self._start_user_timer()

        # this allows execution to keep going
        asyncio.ensure_future(_run())

    async def stop_socket(self, conn_key: str) -> None:
        """Stop a websocket given the connection key

        :param conn_key: Socket connection key
        :type conn_key: string

        :returns: connection key string if successful, False otherwise
        """
        if conn_key not in self._conns:
            return

        # disable reconnecting if we are closing
        await self._conns[conn_key].cancel()
        del self._conns[conn_key]

        # check if we have a user stream socket
        if len(conn_key) >= 60 and conn_key[:60] == self._user_listen_key:
            await self._stop_user_socket()

    async def _stop_user_socket(self) -> None:
        if not self._user_listen_key:
            return
        # stop the timer
        if self._user_timer:
            self._user_timer.cancel()
        self._user_timer = None
        # close the stream
        await self._client.stream_close(listenKey=self._user_listen_key)
        self._user_listen_key = None

    async def close(self) -> None:
        """Close all connections

        """
        keys = set(self._conns.keys())
        for key in keys:
            await self.stop_socket(key)

        self._conns = {}

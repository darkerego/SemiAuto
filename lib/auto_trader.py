import re
import time

import lib.exceptions
# import fibber
from lib import sql_lib, logmod
from lib import tally
from lib.exceptions import *
from lib.func_timer import exit_after
from lib.score_keeper import scores
from trade_engine.aligning_sar import TheSARsAreAllAligning
from trade_engine.stdev_aggravator import FtxAggratavor
from utils.colorprint import NewColorPrint

# from lib import fibber

try:
    import thread
except ImportError:
    import _thread as thread

debug = True

import threading

sql = sql_lib.SQLLiteConnection()
tally = tally.Tally(sql)


class AutoTrader:
    """
    Automatic Trade Engine. Automatically sets stop losses and take profit orders. Trailing stops are used
    unless otherwise specified.
    """
    """
    stop_loss, _take_profit, use_ts=True, ts_pct=0.05, reopen=False, period=300, ot='limit',
                 max_open_orders=None, position_step_size=0.02, disable_stop_loss=False, show_tickers=True,
                 close_method='market', relist_iterations=100, hedge_mode=False, hedge_ratio=0.5,
                 max_collateral=0.5, position_close_pct=1, chase_close=0, chase_reopen=0, update_db=False,
                 anti_liq=False,
                 min_score=0.0, check_before_reopen=False, mitigate_fees=False, confirm=False, tp_fib_enable=False,
                 tp_fib_res=300, sar_sl=0, auto_stop_only=False, mm_mode=False, mm_long_market=None,
                 mm_short_market=None, mm_spread=0.0,
                 long_new_listings=False, short_new_listings=False, new_listing_percent=0, incremental_enter=False
    """

    def __init__(self, api, args):
        # self.trade_logger = TradeLog()
        self.args = args
        self.listings_checked = []
        self.long_new_listings = self.args.long_new_listings
        self.short_new_listings = self.args.short_new_listings
        self.new_listing_percent = self.args.new_listing_percent
        self.position_fib_levels = None
        self.cp = NewColorPrint()
        self.up_markets = {}
        self.down_markets = {}
        self.trend = 'N/A'
        self.auto_stop_only = self.args.auto_stop_only
        self.incrmental_enter = self.args.incremental_enter
        self.show_tickers = self.args.show_tickers
        self.stop_loss = self.args.stop_loss_pct
        self._take_profit = self.args.take_profit_pct
        self.tp_fib_enable = self.args.tp_fib_enable
        self.tp_fib_res = self.args.tp_fib_res
        #self.use_ts = self.args.use_ts
        self.trailing_stop_pct = self.args.ts_offset
        self.api = api
        self.confirm = self.args.confirm
        self.sql = sql_lib.SQLLiteConnection('blackmirror.sqlite')
        self.logger = logmod.CustomLogger(log_file='autotrader.log')
        self.logger.setup_file_handler()
        self.logger = self.logger.get_logger()
        # self.anti_liq_api = AntiLiq(self.api, self.api.getsubaccount())

        self.anti_liq_api = None
        self.fib_api = None
        self.tally = tally
        self.sar_sl = self.args.sar_sl
        self.relist_period = args.relist_period
        self.ta_engine = TheSARsAreAllAligning(debug=False)
        self.accumulated_pnl = 0
        self.position_sars = []
        self.pnl_trackers = []
        self.sar_dict = {}
        self.lock = threading.Lock()
        self.lock2 = threading.Lock()
        self.position_close_pct = self.args.position_close_pct
        #self.chase_close = self.args.chase_close
        #self.chase_reopen = self.args.chase_reopen
        self.min_score = self.args.min_score
        self.check_before_reopen = self.args.check_before_reopen
        self.mitigate_fees = self.args.mitigate_fees
        self.total_contacts_trade = 0.0
        self.reopen = self.args.reopen_method
        self.close_method = self.args.close_method
        self.period = self.args.increment_period
        self.order_type = self.args.order_type
        self.agg = FtxAggratavor()
        self.future_stats = {}
        self.position_times = {}
        self.alert_map = []
        self.alert_up_levels = [0.25, 2.5, 5, 10, 12.5, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100]
        self.alert_down_levels = [-0.25, -2.5, -5, -10, -12.5, -15, -20, -25, -30, -40, -50 - 60, -70, -80, -90, -100]

        self.max_open_orders = self.args.num_open_orders
        self.position_step_size = self.args.position_step_size
        self.disable_stop_loss = self.args.disable_stop_loss
        self.relist_iterations = self.args.relist_iterations
        self.iter = 0
        self.hedge_mode = self.args.hedge_mode
        self.hedge_ratio = self.args.hedge_ratio
        self.anti_liq = self.args.anti_liq
        self.max_collateral = self.args.max_collateral
        self.delta_weight = None
        self.start_time = time.time()
        self.balance_start = 0.0

        self.relist_iter = {}
        self.update_db = self.args.update_db
        self.open_positions = {}
        self.ta_scores = scores
        self.mm_mode = self.args.mm_mode
        self.mm_long_market = self.args.mm_long_market
        self.mm_short_market = self.args.mm_short_market
        self.mm_spread = self.args.mm_spread
        self.candle_time_tuples = {}
        #self.lock = threading.Lock()
        if self.reopen:
            self.cp.yellow('Reopen enabled')
        if self.stop_loss > 0.0:
            self.stop_loss = self.stop_loss * -1

        if self.hedge_mode:
            if self.hedge_ratio < 0:
                self.delta_weight = 'short'
            else:
                self.delta_weight = 'long'
            self.cp.green(f'[~] Hedged Trading Mode Enabled.')
            self.cp.yellow(f'[/] Hedge Ratio: {self.hedge_ratio}, Delta: {self.delta_weight}')
        if not self.confirm:
            self.cp.red('[!] Monitor Only enabled. Will NOT trade.')

    def sanity_check(self, positions):
        print(f'[~] Performing sanity check ...')
        for pos in positions:
            if float(pos['collateralUsed'] != 0.0) or float(pos['longOrderSize']) > 0 or float(
                    pos['shortOrderSize']) < 0:
                instrument = pos['future']
                self.api.cancel_orders(market=instrument, limit_orders=True)

    def api_trailing_stop(self, market, qty, entry, side, offset=.25, ts_o_type='market'):
        """
        {
              "market": "XRP-PERP",
              "side": "sell",
              "trailValue": -0.05,
              "size": 31431.0,
              "type": "trailingStop",
              "reduceOnly": false,
            }
        """
        entry_price = entry
        qty = qty
        self.logger.info('Trailing stop triggered')
        if side == 'buy':
            current_price = self.api.get_ticker(market=market)[1]
            trail_value = (current_price - entry) * self.trailing_stop_pct * -1
            # offset_price = (float(current_price) - float(entry_price)) * (1-offset)
            offset_price = current_price - (current_price - entry) * offset
            text = f'Trailing sell stop for long position, type {ts_o_type}'
            # self.cp.yellow(f'[~] Taking {self.position_close_pct}% of profit ..')

            # qty = qty * (self.take_profit_pct / 100)
            # qty = qty * -1
            opp_side = 'sell'
            self.cp.green(
                f'Trailing Stop for long position of entry price: {entry_price} triggered: offset: {offset_price}'
                f' current price: {current_price}, qty: {qty}')

            ret = self.api.trailing_stop(market=market, side=opp_side, trail_value=trail_value, size=float(qty),
                                         reduce_only=True)
            #print(ret)
            return ret

        else:
            # short position, so this will be a buy stop
            # side = 'buy'
            current_price = self.api.get_ticker(market=market)[0]
            trail_value = (entry - current_price) * self.trailing_stop_pct
            # offset_price = (float(current_price) + float(offset))
            offset_price = current_price + (entry - current_price) * offset
            text = f'Trailing buy stop for short position, type {ts_o_type}'

            opp_side = 'buy'
            self.cp.red(
                f'Trailing Stop for short position of entry price: {entry_price} triggered: offset price {offset_price}'
                f' current price: {current_price}, qty: {qty}')

            ret = self.api.trailing_stop(market=market, side=opp_side, trail_value=trail_value, size=float(qty),
                                         reduce_only=True)
            #print(ret)
            return ret

    def trailing_stop(self, market, qty, entry, side, offset=.25, ts_o_type='market'):
        """
         Place a trailing stop order to ensure profit is taken from current position,
         Stop direction determined by current position, so there is no need to pass a negative offset, but
         if the user does then we correct it by `offset * -1`
        :param offset: integer representing how many dollars to trail the stop behind the current position


        """

        entry_price = entry
        qty = qty
        self.logger.info('Trailing stop triggered')
        if side == 'buy':
            # side = 'sell'
            # long position, so this will be a sell stop
            current_price = self.api.get_ticker(market=market)[1]
            trail_value = (current_price - entry) * self.trailing_stop_pct * -1
            # offset_price = (float(current_price) - float(entry_price)) * (1-offset)
            offset_price = current_price - (current_price - entry) * offset
            text = f'Trailing sell stop for long position, type {ts_o_type}'
            # self.cp.yellow(f'[~] Taking {self.position_close_pct}% of profit ..')

            # qty = qty * (self.take_profit_pct / 100)
            # qty = qty * -1
            opp_side = 'sell'
            self.cp.green(
                f'Trailing Stop for long position of entry price: {entry_price} triggered: offset price {offset_price}'
                f' current price: {current_price}')
        else:
            # short position, so this will be a buy stop
            # side = 'buy'
            current_price = self.api.get_ticker(market=market)[0]
            trail_value = (entry - current_price) * self.trailing_stop_pct
            # offset_price = (float(current_price) + float(offset))
            offset_price = current_price + (entry - current_price) * offset
            text = f'Trailing buy stop for short position, type {ts_o_type}'

            opp_side = 'buy'
            self.cp.red(
                f'Trailing Stop for short position of entry price: {entry_price} triggered: offset price {offset_price}'
                f' current price: {current_price}')

        while True:
            if side == "sell":
                sell_price = self.api.get_ticker(market=market)[1]
                if (float(sell_price) - float(offset)) > float(offset_price):
                    offset_price = float(sell_price) - float(offset)
                    self.cp.purple("New low observed: %.8f Updating stop loss to %.8f" % (sell_price, offset_price))
                elif float(sell_price) <= float(offset_price):
                    sell_price = self.api.get_ticker(market=market)[1]
                    """if tschase:
                        self.logger.info(f'Chasing sell order ... max chase: {max_chase}')
                        self.logger.info("Sell triggered: %s | Price: %.8f | Stop loss: %.8f" % (ts_o_type, sell_price,
                                                                                                 offset_price))
                        chaser = threading.Thread(target=self.limit_chase, args=(qty, max_chase, True))
                        chaser.start()
                    "else:"""
                    self.cp.purple("Buy triggered: %s | Price: %.8f | Stop loss: %.8f" % (ts_o_type, sell_price,
                                                                                          offset_price))
                    ret = self.api.buy_market(market=market, qty=float(qty),
                                              ioc=False, reduce=True, cid=None)
                    self.logger.debug(ret)

                    self.triggered = False
                    return True

            if side == "buy":
                current_price = self.api.get_ticker(market=market)[0]
                if (float(current_price) + float(offset)) < float(offset_price):
                    offset_price = float(current_price) + float(offset)
                    print(offset_price)
                    self.cp.purple(
                        "New high observed: %.8f Updating stop loss to %.8f" % (current_price, offset_price))
                elif float(current_price) >= float(offset_price):
                    current_price = self.api.get_ticker(market=market)[0]
                    """if tschase:
                        self.logger.info(f'Chasing buy order ... max chase: {max_chase}')
                        self.logger.info("Sell triggered: %s | Price: %.8f | Stop loss: %.8f" % (ts_o_type, current_price,
                                                                                                 offset_price))
                        chaser = threading.Thread(target=self.limit_chase, args=(qty, max_chase, True))
                        chaser.start()
                    else:"""
                    self.cp.purple("Sell triggered: %s | Price: %.8f | Stop loss: %.8f" % (ts_o_type, current_price,
                                                                                           offset_price))
                    # ret = self.api.new_order(market=market, side=opp_side, size=qty, _type='market')
                    ret = self.api.sell_market(market=market, qty=float(qty),
                                               ioc=False, reduce=True, cid=None)
                    # ret = self.rest.place_order(market=market, side=opp_side, size=qty, type='market', reduce_only=True, ioc=False)
                    self.logger.debug(ret)

                    self.triggered = False
                    return True

    def stop_loss_order(self, market, side, size):
        # self.logger.info(f'Stop loss triggered for {market}, size: {size}, side: {side}')
        a, b, l = self.api.rest_ticker(market)
        if size < 0.0:
            size = size * -1
        # self.tally.loss()
        if side == 'buy':
            # market sell # market, qty, reduce, ioc, cid
            self.cp.red('[!] Stop hit!')
            self.cancel_limit_side(side, market)
            ret = self.api.sell_market(market=market, qty=size, reduce=True, ioc=False, cid=None)

            if ret.get('id'):
                tally.loss()
                self.log_order(market=market, price=b, trigger_price=0.0, offset=0.0,
                               _type='limit', qty=size, order_id={ret.get('id')}, status='open',
                               text='%s stop via sell market', side='sell')
            return ret
        else:
            # market buy # marekt, qty, reduce, ioc, cid
            self.cp.red('[!] Stop hit!')

            ret = self.api.buy_market(market=market, qty=size, reduce=True, ioc=False, cid=None)
            if ret.get('id'):
                tally.loss()
                self.log_order(market=market, price=a, trigger_price=0.0, offset=0.0,
                               _type='limit', qty=size, order_id={ret.get('id')}, status='open',
                               text='%s stop via buy market', side='buy')
            return ret

    def api_stop_loss(self, market, side, size, limit_price=None):
        if limit_price is None:
            self.api.stop_loss(market, side, size, reduce_only=True)
        else:
            self.api.stop_loss(market, side, size, reduce_only=True)

    def api_take_profit(self, market, side, size, trigger_price, order_price=None, reduce_only=True):

        self.api.take_profit(market, side, size, trigger_price, order_price, reduce_only)

    def take_profit(self, market: str, side: str, entry: float, size: float, order_type: str = 'limit'):
        ticker = self.api.get_ticker(market=market)
        bid = price = float(ticker[0])
        ask = inverse = float(ticker[1])

        if side == 'buy':
            opp_side = 'sell'
            trail_value = ((ask - entry) * self.trailing_stop_pct) * -1
            if self.close_method == 'trailing':
                self.cp.white_black(f'[฿] Sending a trailing stop: {trail_value}')
                ret = self.api_trailing_stop(market=market, side=side, qty=size, offset=self.trailing_stop_pct,
                                             entry=entry,
                                             ts_o_type=self.order_type)
                if ret:
                    print('Success at take profit')
                    # self.wins += 1
                    return ret
            else:

                if order_type == 'market':  # market, qty, reduce, ioc, cid):
                    self.cp.yellow(f'[฿] Sending a market order, side: {opp_side}, price: {price}')
                    ret = self.api.sell_market(market=market, qty=size, ioc=False, reduce=True)
                    return ret
                else:  # market, qty, price=None, post=False, reduce=False, cid=None):
                    self.cp.purple(f'[฿] Sending a limit order, side: {opp_side}, price: {price}')
                    ret = self.api.sell_limit(market=market, qty=size, price=ask, reduce=True)
                    # self.wins += 1
                    return ret
        else:  # side == sell
            opp_side = 'buy'
            trail_value = (entry - bid) * self.trailing_stop_pct
            if self.close_method == 'trailing':
                self.cp.green(f'[฿] Sending a trailing stop: {trail_value}')
                # executor = ThreadPoolExecutor(max_workers=5)
                # ret = executor.submit(self.trailing_stop, market=market, side=opp_side, qty=float(o_size),
                #                      entry=float(entry), offset=float(self.trailing_stop_pct))
                ret = self.api_trailing_stop(market=market, side=side, qty=size, offset=self.trailing_stop_pct,
                                             entry=entry,
                                             ts_o_type=self.order_type)

                if ret:
                    self.cp.purple(f'[~] Success at taking profit.')
                return ret

            else:

                if order_type == 'market':
                    self.cp.purple(f'[฿] Sending a market order, side: {opp_side}, price: {price}')
                    ret = self.api.buy_market(market=market, side=opp_side, size=size,
                                              _type='market', ioc=False, reduce=True)
                    # self.tally.win()
                    return ret
                    # return ret
                else:
                    self.cp.purple(f'[฿] Sending a limit order, side: {opp_side}, price: {price}')
                    ret = self.api.buy_limit(market=market, qty=size, price=ask, reduce=True)
                    # self.wins += 1
                    return ret
        self.cp.red(f"[฿]: ({ret}")

        return ret

    def take_profit_wrap(self, market: str, side: str, entry: float, size: float, order_type: str = 'limit'):

        open_orders = self.api.rest_get_open_orders(market=market)
        open_buy = []
        open_sell = []
        if side == 'buy':
            opp_side = 'sell'
        elif side == 'sell':
            opp_side = 'buy'

        for o in open_orders:
            _side = o['side']
            if _side == 'buy':
                open_buy.append(o)
            if _side == 'sell':
                open_sell.append(o)
        if self.close_method == 'increment':
            if len(open_orders):
                print(f'[~] {len(open_orders)} orders on market: {market} open currently ... ')
                if len(open_orders):
                    if self.candle_time_tuples.get(market):
                        ctt = self.candle_time_tuples.get(market)
                        print('ctt:', ctt)
                        p = ctt[0]
                        c = ctt[1][0]
                        current_c = self.candle_close(p)
                        if current_c[0] > c:
                            self.cp.blue(f'[+] Candle of res {p} has closed, relisting!')
                            self.api.cancel_orders(market=market, limit_orders=True)
                        else:
                            self.cp.yellow(f'[-] Waiting for candle close to relist: {current_c[1]} seconds...')
                            return

        if self.close_method == 'increment':
            # if run_now:

            return self.increment_orders(market=market, side=opp_side, qty=size, period=self.period, reduce=True)
        elif self.close_method == 'market' or self.close_method == 'limit' or 'trailing':
            return self.take_profit(market=market, side=side, entry=entry, size=size, order_type=order_type)

    def re_open_limit(self, market, side, qty):
        for _ in range(3):
            bid, ask, last = self.api.get_ticker(market=market)
            if side == 'buy':  # market, qty, price=None, post=False, reduce=False, cid=None):
                ret = self.api.buy_limit(market=market, qty=qty, price=bid, post=False, reduce=False)
                if ret['id']:
                    self.log_order(market=market, price=bid, trigger_price=0.0, offset=0.0,
                                   _type='limit', qty=qty, order_id={ret.get('id')}, status='open',
                                   text='%s reopen buy', side='buy')
                    return ret
            elif side == 'sell':
                ret = self.api.sell_limit(market=market, qty=qty, price=ask, post=False, reduce=False)
                if ret['id']:
                    self.log_order(market=market, price=bid, trigger_price=0.0, offset=0.0,
                                   _type='limit', qty=qty, order_id={ret.get('id')}, status='open',
                                   text='%s reopen sell', side='sell')
                    return ret

    def re_open_market(self, market, side, qty):
        a, b, l = self.api.get_ticker(market=market)
        for i in range(3):
            if side == 'buy':  # market, qty, price=None, post=False, reduce=False, cid=None):
                ret = self.api.buy_market(market=market, qty=qty, reduce=False, ioc=False, cid=None)

                if ret['id']:
                    self.log_order(market=market, price=b, trigger_price=0.0, offset=0.0,
                                   _type='limit', qty=qty, order_id={ret.get('id')}, status='open',
                                   text='%s reopen buy', side='buy')
                    return ret
            if side == 'sell':
                ret = self.api.sell_market(market=market, qty=qty, reduce=False, ioc=False, cid=None)
                if ret['id']:
                    self.log_order(market=market, price=a, trigger_price=0.0, offset=0.0,
                                   _type='limit', qty=qty, order_id={ret.get('id')}, status='open',
                                   text='%s reopen sell', side='sell')
                    return ret

    def cancel_limit_side(self, side, market):
        open_order_count = self.api.rest_get_open_orders(market=market)
        if len(open_order_count):
            for o in open_order_count:
                o_side = o.get('side')
                if o_side == side:
                    self.api.cancel_order('id')


    def relist_orders(self, market, side, reduce=False):
        """
        {'id': 187482882707, 'clientId': None, 'market': 'EGLD-PERP', 'type': 'limit', 'side': 'buy', 'price': 52.33,
        'size': 0.29, 'status': 'open', 'filledSize': 0.0, 'remainingSize': 0.29, 'reduceOnly': False,
        'liquidation': False, 'avgFillPrice': None, 'postOnly': False, 'ioc': False, 'createdAt':
        '2022-10-03T20:31:15.849650+00:00', 'future': 'EGLD-PERP'}
        """

        open_order_count = 0
        current_orders_qty = 0.0

        self.cp.yellow(f'[~] Calculating and relisting for market: {market}, side: {side}, reduce: {reduce} .... ')
        open_order_count = self.api.rest_get_open_orders(market=market)

        for o in open_order_count:
            o_side = o.get('side')
            o_reduce = o.get('reduceOnly')
            o_market = o.get('market')
            if o_side == side:
                if o_market == market:
                    if o_reduce == reduce:
                        open_order_count += 1
                        current_orders_qty += o.get('size')
        self.cp.purple(f'[i] Relisting (current_buy_orders_qty) orders ...')
        self.cancel_limit_side(side=side, market=market)
        return self.increment_orders(market=market, side=side, qty=current_orders_qty, reduce=reduce)

    def increment_orders(self, market, side, qty, period, reduce=False, text=None):
        current_size = 0
        open_buy_order_count = 0
        open_sell_order_count = 0
        current_buy_orders_qty = 0
        current_sell_orders_qty = 0
        open_order_count = self.api.rest_get_open_orders(market=market)

        for o in open_order_count:
            o_side = o.get('side')
            if o_side == 'buy':
                open_buy_order_count += 1
                current_buy_orders_qty += o.get('size')
            else:
                open_sell_order_count += 1
                current_sell_orders_qty += o.get('size')
            # current_size
        self.cp.yellow(
            f'[i] Open Orders: {len(open_order_count)}, Qty Given: {qty}, Current open buy Qty: {current_buy_orders_qty}, current open sell qty: {current_sell_orders_qty}')

        # if len(open_order_count) > self.max_open_orders * 2:
        #    self.api.cancel_orders(market=market)

        buy_orders = []
        sell_orders = []
        max_orders = self.max_open_orders
        stdev, candle_open = self.agg.get_stdev(symbol=market, period=period)
        self.cp.yellow(f'Standard deviation: {stdev}')
        increment_ = stdev / max_orders

        # o_qty = qty / max_orders
        o_qty = qty

        # self.cp.red(f'[!] Killing {open_order_count} currently open orders...')
        # while qty > (qty * 0.95):
        buy_order_que = []
        sell_order_que = []
        if side == 'buy':
            if open_buy_order_count >= self.max_open_orders:
                print(f'Not doing anything as max orders: {self.max_open_orders} ..')
                return
            else:
                print(f'Max is : {self.max_open_orders}')
                self.candle_time_tuples[market] = (period, self.candle_close(period))
            min_qty = self.future_stats[market]['min_order_size']
            bid, ask, last = self.api.get_ticker(market=market)
            # print(bid, ask, last)
            # last_order_price = bid - (deviation * self.position_step_size)
            for i in range(max_orders):
                qty -= qty / 2
                if qty < min_qty:
                    if i == 0:
                        self.cp.red(f'[!] Order size {qty} is too small for {market}, min size: {min_qty}')
                        return False
                    else:
                        buy_orders[-1] += qty
                        break
                else:
                    buy_orders.append(qty)

            buy_orders = [x for x in buy_orders.__reversed__()]
            print(buy_orders)
            # buy_orders.reverse()
            c = 1
            for x, i in enumerate(buy_orders):
                # if c == 1:
                #    next_order_price = bid
                #    buy_order_que.append(['buy', i, next_order_price, market, 'limit'])
                # else:
                next_order_price = ((bid + ask) / 2) - (increment_ * x)
                # next_order_price = bid - (stdev * self.position_step_size) * c
                buy_order_que.append(['buy', i, next_order_price, market, 'limit'])
                c += 1
                self.cp.yellow(
                    f'[o] Placing new {side} order of size {i} on market {market} at price {next_order_price}')
                for x in range(10):  # market, qty, price=None, post=False, reduce=False, cid=None):
                    try:
                        status = self.api.buy_limit(market=market, price=next_order_price, qty=i,
                                                    reduce=reduce, cid=None)

                    except Exception as err:
                        print(f'[!] Error placing limit order: {err}')
                    else:
                        if status:
                            self.cp.red(f"[฿]: {status.get('id')}")
                            self.log_order(market=market, price=next_order_price, trigger_price=0.0, offset=0.0,
                                           _type='limit', qty=i, order_id={status.get('id')}, status='open',
                                           text=f'{text} increment buy', side='buy')
                            break
                        else:
                            time.sleep(0.25)
            # self.cp.debug(f'Debug: : Buy Orders{buy_orders}')
            return True


        else:
            if open_sell_order_count >= (self.max_open_orders):
                print(f'Not doing anything as max open sell orders: max{self.max_open_orders}')
                return
            else:
                print(f'Max is: {self.max_open_orders}')
                self.candle_time_tuples[market] = (period, self.candle_close(period))
            min_qty = self.future_stats[market]['min_order_size']
            bid, ask, last = self.api.get_ticker(market=market)
            # print(bid, ask, last)
            # last_order_price = bid - (deviation * self.position_step_size)
            for i in range(max_orders):
                qty -= qty / 2
                if qty < min_qty:
                    if i == 0:
                        self.cp.red(f'[!] Order size {qty} is too small for {market}, min size: {min_qty}')
                        return False
                    else:
                        sell_orders[-1] += qty
                        break
                else:
                    sell_orders.append(qty)

            sell_orders = [x for x in sell_orders.__reversed__()]
            print(sell_orders)
            c = 1
            # sell_orders.
            for x, i in enumerate(sell_orders):
                # if i == 1:
                #    next_order_price = ask
                #    sell_order_que.append(['sell', i, next_order_price, market, 'limit'])
                # else:
                # next_order_price = ask + (stdev * self.position_step_size) * c
                next_order_price = ((bid + ask) / 2) + (increment_ * x)
                sell_order_que.append(['sell', i, next_order_price, market, 'limit'])
                c += 1
                self.cp.yellow(
                    f'[o] Placing new {side} order of size {i} on market {market} at price {next_order_price}')
                for x in range(10):  # market, qty, price=None, post=False, reduce=False, cid=None):
                    try:
                        status = self.api.sell_limit(market=market, price=next_order_price, qty=i,
                                                     reduce=reduce, cid=None)
                    except Exception as fuck:
                        print(f'[!] Error placing sell order: ', fuck)
                    else:
                        if status:
                            self.cp.purple(f"[฿]: {status['id']}")
                            self.log_order(market=market, price=next_order_price, trigger_price=0.0, offset=0.0,
                                           _type='limit', qty=i, order_id={status.get('id')}, status='open',
                                           text=f'{text} increment sell', side='sell')
                            break
                        else:
                            time.sleep(0.25)
            # self.cp.debug(f'Debug: : Sell Orders{sell_orders}')
            return True

    def reopen_pos(self, market, side, qty, period=None, info=None):
        coll = info["collateral"]
        free_coll = info["freeCollateral"]
        new_qty = 0
        self.cp.yellow(f'[~] Taking {self.position_close_pct}% of profit ..')
        # qty = (qty * self.position_close_pct)
        if self.hedge_mode:
            self.cp.blue(f'[⌖] Hedge Mode Enabled ... ')
            if self.hedge_ratio > 0:
                # delta long
                max_allocate_short = (coll * (1 - float(self.hedge_ratio)) * self.max_collateral)
                max_allocate_long = (coll * (float(self.hedge_ratio)) * self.max_collateral)
                if qty > max_allocate_long:
                    new_qty = (free_coll * (float(self.hedge_ratio)) * self.max_collateral)
                    self.cp.red(f'[~] Δ+ Notice: recalculated position size from {qty} to {new_qty}  ... ')
                elif qty > max_allocate_short:
                    new_qty = (free_coll * (1 - float(self.hedge_ratio)) * self.max_collateral)
                    self.cp.red(f'[~] Δ+ Notice: recalculated position size from {qty} to {new_qty}  ... ')
            elif self.hedge_ratio < 0:
                # delta short
                max_allocate_short = (coll * (1 - float(self.hedge_ratio) * -1) * self.max_collateral)
                max_allocate_long = (coll * (float(self.hedge_ratio) * -1) * self.max_collateral)

                if qty > max_allocate_long:
                    new_qty = (free_coll * (float(self.hedge_ratio) * -1) * self.max_collateral)  # invert if negative
                    self.cp.red(f'[~] Δ- Notice: recalculated position size from {qty} to {new_qty} from  ... ')
                elif qty > max_allocate_short:
                    new_qty = (free_coll * (1 - float(self.hedge_ratio)) * self.max_collateral)
                    self.cp.red(f'[~] Δ- Notice: recalculated position size from {qty} to {new_qty}  ... ')

            else:
                max_allocate = (coll * 0.5 * self.max_collateral)
                if qty > max_allocate:
                    new_qty = (free_coll * 0.5 * self.max_collateral)
                    self.cp.red(f'[~] Δ Notice: recalculated position size from {qty} to {new_qty}  ... ')
        if new_qty:
            qty = new_qty

        if self.check_before_reopen:
            ta_score = self.ta_scores.get(market)
            if ta_score:
                if ta_score.get('status') == 'closed':
                    print(f'[!] Not reopening because the signal has closed.')
                    return False
                if ta_score.get('score') <= self.min_score:
                    print(f'[!] Not reopening because the score is too low.')
                    return False
            else:
                print(f'[!] Score not available. Bailing.')
                return
        if self.order_type == 'limit' and self.reopen != 'increment':
            return self.re_open_limit(market, side, qty)
        if self.order_type == 'market' and self.reopen != 'increment':
            return self.re_open_market(market, side, qty)
        if self.reopen == 'increment':
            return self.increment_orders(market, side, qty, period, text='reopen')

    def pnl_calc(self, qty, sell, buy, side, cost, future_instrument, fee=0.00019, double_check=False):
        """
        Profit and Loss Calculator - assume paying
         two market fees (one for take profit, one for re-opening). This way we can
         set tp to 0 and run as market maker and make something with limit orders.
        """
        if double_check:
            ask, bid, last = self.api.rest_ticker(market=future_instrument)
            if side == 'buy':
                buy = bid
            else:
                buy = ask

        if fee <= 0:
            pnl = float(qty * (sell - buy) * (1 - fee))
        else:
            pnl = float(qty * (sell - buy) * (1 - (fee * 2)))

        if pnl is not None:  # pythonic double negative nonsense
            if side == 'buy':
                try:
                    if pnl <= 0.0:
                        self.cp.red(f'[🔻] Negative PNL {pnl} on position {future_instrument}')
                    else:
                        self.cp.green(f'[🔺] Positive PNL {pnl} on position {future_instrument}')
                except Exception as err:
                    print('Error calculating PNL: ', err)
                else:
                    try:
                        pnl_pct = (float(pnl) / float(cost)) * 100
                    except Exception as err:
                        print('Error calculating PNL%: ', err)
                    else:
                        return pnl, pnl_pct
            else:
                try:
                    pnl_pct = (float(pnl) / float(cost * -1)) * 100
                except Exception as err:
                    print('DEBUG Error calculating PNL line 683: ', err)
                else:
                    return pnl, pnl_pct
            return 0.0, 0.0

    def check_pnl(self, side, future_instrument, size, avg_open_price, cost, takerFee, double_check=False):

        if size is None:
            size = 0
        if avg_open_price is None:
            avg_open_price = 0
        #print(side, future_instrument, size, avg_open_price, cost, takerFee, double_check)
        if side == 'buy':

            self.trailing_stop_pct = self.trailing_stop_pct * -1
            ask, bid, last = self.api.get_ticker(market=future_instrument)
            if not ask:
                return
            try:
                pnl, pnl_pct = self.pnl_calc(qty=(size * -1), sell=avg_open_price, buy=bid, side=side,
                                             cost=cost, future_instrument=future_instrument, fee=takerFee,
                                             double_check=double_check)
            except ZeroDivisionError:
                pass
            except Exception as err:
                print(err)
            else:
                return pnl, pnl_pct


        else:
            # short position
            side = 'sell'
            ask, bid, last = self.api.get_ticker(market=future_instrument)
            if not bid:
                return
            pnl, pnl_pct = self.pnl_calc(qty=(size * -1), sell=avg_open_price, buy=ask, side=side,
                                         cost=cost, future_instrument=future_instrument, fee=takerFee,
                                         double_check=double_check)
            try:
                if pnl <= 0.0:
                    self.cp.red(f'[🔻] Negative PNL {pnl} on position {future_instrument}')
                else:
                    self.cp.green(f'[🔺] Positive PNL {pnl} on position {future_instrument}')
            except ZeroDivisionError:
                pass
            except Exception as err:
                self.logger.error('DEBUG', err)

                pass
            else:
                return pnl, pnl_pct

    def log_order(self, market, price, trigger_price=0.0, offset=0.0, _type='limit', qty=0.0, order_id=None,
                  status='open', text=None, side=None):
        _sql = (market, side, price, trigger_price, offset, _type, order_id, status, qty, text, time.time())
        self.sql.append(_sql, 'orders')

    def check_new_listings(self, info, side=None):
        print('[~] Enumerating new listings .. ')
        fut = self.api.futures()
        current_listings = self.sql.get_list(table='listings')
        # print(current_listings)
        for _ in fut:
            listing = _.get('name')
            # self.cp.random_color(f'Checking listing...{listing}')
            if current_listings.__contains__(listing):
                pass
            else:
                if self.listings_checked.__contains__(listing):
                    pass
                else:
                    if re.findall('BTC-MOVE', listing):
                        pass
                    else:
                        self.sql.append(value=listing,
                                    table='listings')  # TODO: Keep track of index price data so that we can automatically
                    # trade trending assets!

                    self.cp.alert(f'[🎲🎲🎲] NEW LISTING DETECTED: {listing}, lets roll those fuckin\' dice! WOOT!')

                    if side is not None and not self.update_db:
                        l_size = float(info['freeCollateral']) * float(self.new_listing_percent)
                        if l_size > 0:
                            a, b, l = self.api.rest_ticker(listing)
                            qty = float(l_size) / float(l)

                            try:
                                self.api.buy_market(market=listing, qty=qty)
                            except Exception as err:
                                self.cp.red(f'[!] Error attempting to long new listing: {err}.')

    def candle_close(self, interval):
        tm = divmod(time.time(), interval)
        return tm

    def parse(self, pos, info):
        self.iter += 1

        """
        {'future': 'TRX-0625', 'size': 9650.0, 'side': 'buy', 'netSize': 9650.0, 'longOrderSize': 0.0,
        'shortOrderSize': 2900.0, 'cost': 1089.1955, 'entryPrice': 0.11287, 'unrealizedPnl': 0.0, 'realizedPnl':
        -100.1977075, 'initialMarginRequirement': 0.05, 'maintenanceMarginRequirement': 0.03, 'openSize': 9650.0,
        'collateralUsed': 54.459775, 'estimatedLiquidationPrice': 0.11020060583397326, 'recentAverageOpenPrice':
        0.14736589533678757, 'recentPnl': -332.88539, 'recentBreakEvenPrice': 0.14736589533678757,
        'cumulativeBuySize': 9650.0, 'cumulativeSellSize': 0.0}
        """

        future_instrument = pos['future']
        if not self.open_positions.get(future_instrument):
            self.open_positions[future_instrument] = time.time()
        if debug:
            self.cp.white_black(f'[d]: Processing {future_instrument}')
        # fut =
        # print(fut)
        for f in self.api.futures():
            # print(f'Iterating {f}')

            """{'name': 'BTT-PERP', 'underlying': 'BTT', 'description': 'BitTorrent Perpetual Futures', 
            'type': 'perpetual', 'expiry': None, 'perpetual': True, 'expired': False, 'enabled': True, 'postOnly': 
            False, 'priceIncrement': 5e-08, 'sizeIncrement': 1000.0, 'last': 0.0050772, 'bid': 0.00507655, 
            'ask': 0.00508115, 'index': 0.0050384623482894655, 'mark': 0.0050785, 'imfFactor': 1e-05, 'lowerBound': 
            0.00478655, 'upperBound': 0.00532945, 'underlyingDescription': 'BitTorrent', 'expiryDescription': 
            'Perpetual', 'moveStart': None, 'marginPrice': 0.0050785, 'positionLimitWeight': 20.0, 
            'group': 'perpetual', 'change1h': -0.009169837089064482, 'change24h': 0.3340075388434311, 'changeBod': 
            0.11461053925334153, 'volumeUsd24h': 41050555.11565, 'volume': 9253264000.0} """
            mark_price = f['mark']
            index = f['index']
            name = f['name']
            volumeUsd24h = f['volumeUsd24h']
            change1h = f['change1h']
            change24h = f['change24h']
            min_order_size = f['sizeIncrement']
            self.future_stats[name] = {}
            self.future_stats[name]['mark'] = mark_price
            self.future_stats[name]['index'] = index
            self.future_stats[name]['volumeUsd24h'] = volumeUsd24h
            self.future_stats[name]['change1h'] = change1h
            self.future_stats[name]['change24h'] = change24h
            self.future_stats[name]['min_order_size'] = min_order_size

            # if self.tp_fib_enable:
            #    levels = selff
            #    #self.position_fib_levels[future_instrument]

            err = None

            # exit()
            # if not self.ticker_stats.__contains__(name):
            #    name = FutureStat(name=name, price=mark_price, volume=volumeUsd24h)
            #    self.ticker_stats.append(name)
            # else:
            #    p, v = name.update(price=mark_price, volume=volumeUsd24h)

            # if self.show_tickers:
            if f['name'] == future_instrument:
                if debug:
                    self.cp.dark(
                        f"[🎰] [{name}] Future Stats: {change1h}/hour {change24h}/today, Volume: {volumeUsd24h}")
                # print(f'Debug: {f}')
            if float(change1h) > 0.025 and self.show_tickers:
                if float(change24h) > 0:
                    self.cp.ticker_up(
                        f'[🔺]Future {name} is up {change1h} % this hour! and {change24h} today, Volume: {volumeUsd24h}, ')


                else:
                    self.cp.ticker_up(f'[🔺] Future {name} is up {change1h} % this hour!')

            if change1h < -0.025 and self.show_tickers:
                if float(change24h) < 0:
                    self.cp.ticker_down(
                        f'[🔻]Future {name} is down {change1h} % this hour!and {change24h} today, Volume: {volumeUsd24h}!')
                else:
                    self.cp.ticker_down(f'[🔻] Future {name} is down {change1h} % this hour!')

            if change24h > 0:
                self.up_markets[name] = (volumeUsd24h, change1h)
            elif change24h < 0:
                self.down_markets[name] = (volumeUsd24h, change1h)

        if len(self.up_markets) > len(self.down_markets):
            if self.show_tickers:
                self.cp.green('[+] Market Average Trend: LONG')
            self.trend = 'up'
        if len(self.up_markets) == len(self.down_markets):
            if self.show_tickers:
                self.cp.yellow('[~] Market Average Trend: NEUTRAL')
        if len(self.up_markets) < len(self.down_markets):
            if self.show_tickers:
                self.cp.red('[-] Market Average Trend: SHORT')
            self.trend = 'down'

        # if future_instrument in self.symbols:
        collateral_used = pos['collateralUsed']
        cost = pos['cost']
        buy_size = pos['cumulativeBuySize']
        sell_size = pos['cumulativeSellSize']
        size = pos['netSize']
        entry_price = pos['entryPrice']
        liq_price = pos['estimatedLiquidationPrice']
        avg_open_price = pos['recentAverageOpenPrice']
        avg_break_price = pos['recentBreakEvenPrice']
        recent_pnl = pos['recentPnl']
        unrealized_pnl = pos['unrealizedPnl']
        takerFee = info['takerFee']
        makerFee = info['makerFee']
        side = pos['side']
        pnl = 0
        pnl_pct = 0
        tpnl = 0
        tsl = 0
        if size is None:
            size - 0.0




        # For future implantation
        # Are we a long or a short?
        pnl, pnl_pct = self.check_pnl(side, future_instrument, size, avg_open_price, cost, takerFee)
        ask, bid, last = self.api.get_ticker(future_instrument)
        if side == 'buy':
            pos_side = 'sell'
        else:
            pos_side = 'buy'

        self.cp.random_pulse(
            f'[▶] Instrument: {future_instrument}, Side: {side}, Size: {size} Cost: {cost}, Entry: {entry_price},'
            f' Open: {avg_open_price} Liq: {liq_price}, BreakEven: {avg_break_price}, PNL: {recent_pnl}, '
            f'UPNL: {unrealized_pnl}, Collateral: {collateral_used}')
        if recent_pnl is None:
            return
        if self.sar_sl:
            def get_sar_val(market__):
                cc = self.candle_close(self.sar_sl)[0]
                __side, _sar = self.ta_engine.get_sar(market__, int(self.sar_sl))
                self.sar_dict[market__] = (cc, (__side, _sar))
                return __side, _sar

            def sar_get_wrap(market__):
                for i in range(3):
                    try:
                        sar_vals = self.sar_dict.get(market__)
                    except RuntimeError:
                        time.sleep(0.25)
                    else:
                        return sar_vals
                return False

            self.iter = 0
            close_pos = False
            if not self.sar_dict.get(future_instrument):
                _side, sar = get_sar_val(future_instrument)
            else:
                sar_vals = sar_get_wrap(future_instrument)
                if sar_vals:
                    _cc = sar_vals[0]
                    ccc = self.candle_close(self.sar_sl)[0]
                    if ccc > _cc:
                        _side, sar = get_sar_val(future_instrument)
                    else:
                        _side = sar_vals[1][0]
                        sar = sar_vals[1][1]
                else:
                    self.cp.red('[!] Error: could not get sar values ..')

            if side == 'buy':
                if _side == 1:
                    _side = 'long'
                else:
                    ask, bid, last = self.api.get_ticker(future_instrument)
                    current_stop = last - ((sar/100) * self.stop_loss)
                    self.cp.red(f'[SAR]: {sar}, Current stop: {current_stop}')
                    if bid < current_stop:
                        close_pos = True
            else:
                if _side == -1:
                    _side = 'short'
                else:
                    ask, bid, last = self.api.get_ticker(future_instrument)
                    current_stop = last + ((sar / 100) * self.stop_loss)
                    self.cp.red(f'[SAR]: {sar}, Current stop: {current_stop}')
                    if ask > current_stop:
                        close_pos = True

            if close_pos:
                if self.confirm:
                    self.cp.red('[!!] Closing position as the sar is not in our favor!')
                    self.stop_loss_order(market=future_instrument, side=side, size=size * -1)
                    # self.api.cancel_orders(future_instrument)
        if pnl_pct > self._take_profit and not self.auto_stop_only:
            # confirm price via rest

            # print('Recalculating with rest ticker .. ')
            pnl, pnl_pct = self.check_pnl(side, future_instrument, size, avg_open_price, cost, takerFee,
                                          double_check=True)
            if pnl_pct <= self._take_profit:
                # print('Not ok')
                pass
            else:

                print(f'[+] Target profit level of {self._take_profit} reached! Calculating pnl')
                if float(size) < 0.0:
                    size = size * -1

                # o_size = size
                # notational_qty = (o_size * last)
                # self.total_contacts_trade += notational_qty
                # self.tally.increment_contracts(notational_qty)
                new_qty = size * self.position_close_pct
                # print('ok')
                if float(new_qty) < float(self.future_stats[future_instrument]['min_order_size']):
                    new_qty = size
                self.cp.purple(f'Sending {pos_side} order of size {new_qty} , price {last}')
                if not self.confirm:
                    self.cp.red('[!] Not actually trading... ')

                else:

                    try:

                        ret = self.take_profit_wrap(entry=entry_price, side=side, size=new_qty,
                                                    order_type=self.order_type,
                                                    market=future_instrument)
                    except Exception as err:
                        #self.logger.error('Error with take profit wrap:', err)
                        ret = False
                        if re.match(r'^(.*)margin for order(.*)$',
                                    err.__str__()):
                            self.cp.red('[!] Not enough margin!')

                        elif re.match(r'^(.*)Size too small(.*)$', err.__str__()):
                            qty = size
                            self.cp.red('[!] Size too small! Fail ...')

                        elif re.match(r'^(.*)rigger price too(.*)$', err.__str__()):
                            self.cp.red('[!] This stupid trigger price error!')
                        else:
                            self.cp.red(f'[!] Error with order: {err}')
                    else:
                        if ret:
                            self.accumulated_pnl += pnl
                            self.tally.win()

                            self.cp.alert('----------------------------------------------')
                            self.cp.alert(f'Total Session PROFITS: {self.accumulated_pnl}')
                            self.cp.alert('----------------------------------------------')
                            self.cp.green(
                                f'Reached target pnl of {pnl_pct} on {future_instrument}, taking profit... PNL: {pnl}')
                            notational_qty = (new_qty * last)
                            # self.total_contacts_trade += notational_qty
                            self.tally.increment_contracts(notational_qty)
                            if self.anti_liq:
                                self.anti_liq_transfer()

                            print('[🃑] Success')

                        if ret and self.reopen:
                            # self.accumulated_pnl += pnl
                            self.cp.yellow(f'Reopening .... {side} {new_qty}')

                            try:
                                ret = self.reopen_pos(market=future_instrument, side=side, qty=new_qty,
                                                      period=self.period, info=info)
                            except Exception as err:
                                print('err', err)
                                # if re.match(r'^(.*)margin for order(.*)$', err.__str__()):
                                self.cp.red(f'[~] Error with order: {err.__str__()}')
                            else:

                                if ret:
                                    notational_qty = (new_qty * last)
                                    self.total_contacts_trade += notational_qty
                                    self.tally.increment_contracts(notational_qty)
                                    print('[🃑] Success')
        else:
            try:
                tpnl = (self._take_profit / pnl_pct) * pnl
            except ZeroDivisionError:
                pass

            try:
                tsl = (self.stop_loss / pnl_pct) * pnl
            except ZeroDivisionError:
                pass

            self.cp.yellow(
                f'[$]PNL %: {pnl_pct}/Target TP Profit: {self._take_profit}/Target SL Loss: {self.stop_loss}, PNL USD: {pnl}, '
                f'Target PNL USD: ${tpnl}, Target STOP USD: ${tsl}')
            if pnl_pct < self.stop_loss and not self.disable_stop_loss:
                pnl, pnl_pct = self.check_pnl(side, future_instrument, size, avg_open_price, cost, takerFee,
                                              double_check=True)
                if pnl_pct < self.stop_loss and not self.disable_stop_loss:
                    if self.confirm:
                        self.stop_loss_order(market=future_instrument, side=side, size=size * -1)

                    else:
                        self.cp.red('[!] NOT TRADING: Stop Hit.')
                    self.accumulated_pnl -= pnl

    @exit_after(30)
    def position_parser(self, positions, account_info):
        for pos in positions:
            if float(pos['longOrderSize']) == 0 and float(pos['shortOrderSize']) == 0:
                if self.candle_time_tuples.get(pos.get('future')):
                    self.candle_time_tuples.pop(pos.get('future'))

            if float(pos['collateralUsed'] != 0.0) or float(pos['longOrderSize']) > 0 or float(
                    pos['shortOrderSize']) < 0:
                with self.lock2:
                    self.parse(pos, account_info)
            else:
                try:
                    for _ in self.open_positions:
                        if pos['future'] == _:
                            self.open_positions.pop(_)
                except Exception as err:
                    print('ERR', err)
                    pass

    def update_database(self):
        added = 0
        futures = self.api.futures()
        current_listings = self.sql.get_list(table='listings')
        for x in futures:
            name = x.get('name')
            if current_listings.__contains__(name):
                pass
            else:
                self.sql.append(table='listings', value=name)
                added += 1
        print(f'Updated db. Added {added} entries to db.')

    def start_process_(self):
        self.logger.info(f"Starting autotrader at {time.time()}")
        restarts = 0
        _iter = 0
        if self.update_db:
            self.cp.yellow('[~] Updating futures database ... ')
            self.update_database()
            exit()
        while True:
            # print(self.long_new_listings,self.short_new_listings)
            if self.long_new_listings:
                print('[+] Checking new listings!')
                # print(_iter)
                if _iter % 100 == 0:
                    new_side = 'buy'
                    info = self.api.info()
                    self.check_new_listings(info=info, side=new_side)
            for f in self.api.futures():

                """{'username': 'xxxxxxxx@gmail.com', 'collateral': 4541.2686261529458, 'freeCollateral': 
                                                13.534738011297414, 'totalAccountValue': 4545.7817261529458, 'totalPositionSize': 9535.4797, 
                                                'initialMarginRequirement': 0.05, 'maintenanceMarginRequirement': 0.03, 'marginFraction': 
                                                0.07802672286726425, 'openMarginFraction': 0.07527591244130713, 'liquidating': False, 'backstopProvider': 
                                                False, 'positions': [{'future': 'BAT-PERP', 'size': 0.0, 'side': 'buy', 'netSize': 0.0, 'longOrderSize': 
                                                0.0, 'shortOrderSize': 0.0, 'cost': 0.0, 'entryPrice': None, 'unrealizedPnl': 0.0, 'realizedPnl': 
                                                5.59641262, 'initialMarginRequirement': 0.05, 'maintenanceMarginRequirement': 0.03, 'openSize': 0.0, 
                                                'collateralUsed': 0.0, 'estimatedLiquidationPrice': None}, """

                """{'name': 'BTT-PERP', 'underlying': 'BTT', 'description': 'BitTorrent Perpetual Futures', 
                'type': 'perpetual', 'expiry': None, 'perpetual': True, 'expired': False, 'enabled': True, 'postOnly': 
                False, 'priceIncrement': 5e-08, 'sizeIncrement': 1000.0, 'last': 0.0050772, 'bid': 0.00507655, 
                'ask': 0.00508115, 'index': 0.0050384623482894655, 'mark': 0.0050785, 'imfFactor': 1e-05, 'lowerBound': 
                0.00478655, 'upperBound': 0.00532945, 'underlyingDescription': 'BitTorrent', 'expiryDescription': 
                'Perpetual', 'moveStart': None, 'marginPrice': 0.0050785, 'positionLimitWeight': 20.0, 
                'group': 'perpetual', 'change1h': -0.009169837089064482, 'change24h': 0.3340075388434311, 'changeBod': 
                0.11461053925334153, 'volumeUsd24h': 41050555.11565, 'volume': 9253264000.0} """
                mark_price = f['mark']
                index = f['index']
                name = f['name']
                volumeUsd24h = f['volumeUsd24h']
                change1h = f['change1h']
                change24h = f['change24h']
                min_order_size = f['sizeIncrement']
                self.future_stats[name] = {}
                self.future_stats[name]['name'] = name
                self.future_stats[name]['mark'] = mark_price
                self.future_stats[name]['index'] = index
                self.future_stats[name]['volumeUsd24h'] = volumeUsd24h
                self.future_stats[name]['change1h'] = change1h
                self.future_stats[name]['change24h'] = change24h
                self.future_stats[name]['min_order_size'] = min_order_size

                try:
                    info = self.api.info()
                    pos = self.api.positions()


                except KeyboardInterrupt:
                    print('[~] Caught Sigal...')
                    exit(0)

                except Exception as err:
                    _iter = 0
                    self.logger.error(f'Error with parse: {err}')

                else:
                    _iter += 1
                    if _iter == 1:
                        restarts += 1
                        self.cp.purple('[i] Starting AutoTrader,  ...')
                        # self.sanity_check(positions=pos)
                    self.cp.pulse(f'[$] Account Value: {info["totalAccountValue"]} Collateral: {info["collateral"]} '
                                  f'Free Collateral: {info["freeCollateral"]}, Contracts Traded: {self.total_contacts_trade}'
                                  f' Restarts: {restarts}')
                    _tally = self.tally.get()
                    wins = _tally.get('wins')
                    losses = _tally.get('losses')
                    volume = _tally.get('contracts_traded')
                    if wins != 0 or losses != 0:
                        self.cp.white_black(f'[🃑] Wins: {wins} [🃏] Losses: {losses}, Volume: {volume}')
                    else:
                        self.cp.white_black(f'[🃑] Wins: - [🃏] Losses: -, Volume: {volume}')
                    try:

                        self.position_parser(positions=pos, account_info=info)

                    except lib.exceptions.RestartError as fuck:
                        self.logger.error(fuck)
                        print(repr(f'Restart: {fuck} {_iter}'))
                        _iter = 0
                        # break
                    except Exception as fuck:
                        print('error ',fuck)
                    #    self.logger.error(f'Error with position parser: {fuck}')
                    #    _iter = 0
                        # break

    def start_process(self):
        if not self.lock.locked():
            print('Acquiring lock in autotrader')
            self.lock.acquire()
        else:
            print('Could not aquire lock!')
            return
        try:
            self.start_process_()
        except KeyboardInterrupt:
            print('Caught Signal!')
            exit()

    def anti_liq_transfer(self, profit):
        qty_fraction = self._take_profit * 0.1
        self.anti_liq_api.transfer()

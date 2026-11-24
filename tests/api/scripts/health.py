"""对运行中的 Shulan 服务执行一次真实健康检查。"""

import requests

from config.env import SERVER_PORT
from utils.log import getLogger

logger = getLogger(__name__)

BASE_URL = f'http://localhost:{SERVER_PORT}/health'


def main():
    response = requests.get(BASE_URL)

    logger.info(response)
    logger.info(response.status_code)
    logger.info(response.json())


if __name__ == '__main__':
    main()

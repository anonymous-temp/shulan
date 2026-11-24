from config import env
from utils.log import configure_tornado_logging
from utils.log import getLogger
from utils.log import shutdown_logging


def main():
    configure_tornado_logging()
    try:
        getLogger(__name__).info('%s', env.summary())
        import server
        server.run()
    finally:
        shutdown_logging()


if __name__ == '__main__':
    main()

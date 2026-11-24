import dotenv

dotenv.load_dotenv()


def test_report():
    import asyncio
    from services import report

    user_id = "test_user_001"
    session_id = "001_0001"
    res = asyncio.run(report.create_report(user_id, session_id))
    print(res)


def main():
    test_report()


if __name__ == '__main__':
    main()

"""业务模块公共入口的兼容性约束。"""

import unittest


class ModuleApplicationFacadeTest(unittest.TestCase):
    """迁移期门面必须继续指向既有实现。"""

    def test_analysis_application_exports_legacy_services(self) -> None:
        from modules import analysis
        from modules.analysis import application
        from services import analysis as legacy

        self.assertIs(
            application.HealthReportProcessingService,
            legacy.HealthReportProcessingService,
        )
        self.assertIs(
            application.HealthReportInterpreter,
            legacy.HealthReportInterpreter,
        )
        self.assertIs(
            analysis.HealthReportProcessingService,
            application.HealthReportProcessingService,
        )

    def test_chat_application_exports_legacy_services(self) -> None:
        from modules import chat
        from modules.chat import application
        from services import chat as legacy

        self.assertIs(application.ChatSessionManager, legacy.ChatSessionManager)
        self.assertIs(
            application.ChatCustomMessageValidationError,
            legacy.ChatCustomMessageValidationError,
        )
        self.assertIs(chat.ChatSessionManager, application.ChatSessionManager)

    def test_record_application_exports_legacy_services(self) -> None:
        from modules import record
        from modules.record import application
        from services import record as legacy

        self.assertIs(application.QASessionManager, legacy.QASessionManager)
        self.assertIs(
            application.QASessionConflictError,
            legacy.QASessionConflictError,
        )
        self.assertIs(record.QASessionManager, application.QASessionManager)

    def test_health_profile_application_exports_service_and_factories(
            self,
    ) -> None:
        import health_profile as legacy
        from health_profile import application as legacy_application
        from modules import health_profile
        from modules.health_profile import application

        self.assertIs(application.HealthProfileService, legacy.HealthProfileService)
        self.assertIs(
            application.HealthProfileConflictError,
            legacy.HealthProfileConflictError,
        )
        self.assertIs(
            application.create_application_health_profile_service,
            legacy_application.create_application_health_profile_service,
        )
        self.assertIs(
            health_profile.HealthProfileService,
            application.HealthProfileService,
        )

    def test_public_entries_define_explicit_exports(self) -> None:
        from modules import analysis
        from modules import chat
        from modules import health_profile
        from modules import record

        for module in (analysis, chat, record, health_profile):
            with self.subTest(module=module.__name__):
                self.assertTrue(module.__all__)
                self.assertEqual(len(module.__all__), len(set(module.__all__)))
                for name in module.__all__:
                    self.assertTrue(hasattr(module, name), name)


if __name__ == '__main__':
    unittest.main()

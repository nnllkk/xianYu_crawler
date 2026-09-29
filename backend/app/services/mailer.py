import smtplib
from email.message import EmailMessage

from ..config import Settings


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, recipients: list[str], subject: str, body: str) -> None:
        if not self.settings.smtp_host or not self.settings.smtp_from:
            raise RuntimeError("未配置 SMTP_HOST 或 SMTP_FROM")
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port) as smtp:
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(message)

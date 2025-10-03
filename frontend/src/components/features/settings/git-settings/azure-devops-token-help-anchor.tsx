import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export function AzureDevOpsTokenHelpAnchor() {
  const { t } = useTranslation();

  return (
    <p data-testid="azure-devops-token-help-anchor" className="text-xs">
      <a
        href="https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate"
        target="_blank"
        className="underline underline-offset-2"
        rel="noopener noreferrer"
        aria-label={t(I18nKey.AZURE_DEVOPS$TOKEN_HELP_TEXT)}
      >
        {t(I18nKey.AZURE_DEVOPS$TOKEN_HELP_TEXT)}
      </a>
    </p>
  );
}

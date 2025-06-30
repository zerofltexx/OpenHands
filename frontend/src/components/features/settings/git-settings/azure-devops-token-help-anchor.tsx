import React from "react";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { BrandAnchor } from "../../brand-anchor";

export function AzureDevOpsTokenHelpAnchor() {
  const { t } = useTranslation();

  return (
    <BrandAnchor
      href="https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate"
      target="_blank"
      rel="noopener noreferrer"
    >
      {t(I18nKey.GIT$AZURE_DEVOPS_TOKEN_HELP)}
    </BrandAnchor>
  );
}
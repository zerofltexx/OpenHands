import React from "react";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { BrandInput } from "../../brand-input";
import { BrandLabel } from "../../brand-label";
import { AzureDevOpsTokenHelpAnchor } from "./azure-devops-token-help-anchor";

type AzureDevOpsTokenInputProps = {
  name: string;
  isAzureDevOpsTokenSet: boolean;
  onChange: (value: string) => void;
  onAzureDevOpsHostChange: (value: string) => void;
  azureDevOpsHostSet: string | null;
};

export function AzureDevOpsTokenInput({
  name,
  isAzureDevOpsTokenSet,
  onChange,
  onAzureDevOpsHostChange,
  azureDevOpsHostSet,
}: AzureDevOpsTokenInputProps) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-2">
        <div className="flex flex-row gap-2 items-center">
          <BrandLabel htmlFor={name}>
            {t(I18nKey.GIT$AZURE_DEVOPS_TOKEN)}
          </BrandLabel>
          <AzureDevOpsTokenHelpAnchor />
        </div>
        <BrandInput
          id={name}
          name={name}
          type="password"
          placeholder={
            isAzureDevOpsTokenSet
              ? t(I18nKey.GIT$TOKEN_PLACEHOLDER_SET)
              : t(I18nKey.GIT$TOKEN_PLACEHOLDER_UNSET)
          }
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
      <div className="flex flex-col gap-2">
        <BrandLabel htmlFor="azure-devops-host-input">
          {t(I18nKey.GIT$AZURE_DEVOPS_HOST)}
        </BrandLabel>
        <BrandInput
          id="azure-devops-host-input"
          name="azure-devops-host-input"
          type="text"
          placeholder={
            azureDevOpsHostSet || t(I18nKey.GIT$AZURE_DEVOPS_HOST_PLACEHOLDER)
          }
          onChange={(e) => onAzureDevOpsHostChange(e.target.value)}
        />
      </div>
    </div>
  );
}

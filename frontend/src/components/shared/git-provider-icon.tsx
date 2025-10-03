import { FaBitbucket, FaGithub, FaGitlab } from "react-icons/fa6";
import { Provider } from "#/types/settings";
import AzureDevOpsLogo from "#/assets/branding/azure-devops-logo.svg?react";

interface GitProviderIconProps {
  gitProvider: Provider;
  className?: string;
}

export function GitProviderIcon({
  gitProvider,
  className,
}: GitProviderIconProps) {
  return (
    <>
      {gitProvider === "github" && <FaGithub size={14} className={className} />}
      {gitProvider === "gitlab" && <FaGitlab className={className} />}
      {gitProvider === "bitbucket" && <FaBitbucket className={className} />}
      {gitProvider === "azure_devops" && <AzureDevOpsLogo className={className} />}
    </>
  );
}

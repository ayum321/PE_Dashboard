/*
 * ===============================================================================================================
 *                                Copyright 2021, Blue Yonder Group, Inc.
 *                                           All Rights Reserved
 *
 *                               THIS IS UNPUBLISHED PROPRIETARY SOURCE CODE OF
 *                                          BLUE YONDER GROUP, INC.
 *
 *
 *                         The copyright notice above does not evidence any actual
 *                                 or intended publication of such source code.
 *
 * ===============================================================================================================
 */

import {
  PortalMessageService,
  ThemeSwitchRequestMessage,
  Breadcrumbs as BreadcrumbsMessage,
  BreadcrumbInfo,
} from '@jda/lui-portal-utilities';

export const handleThemeRequest = () => {
  const themeSwitchRequestMessage = new ThemeSwitchRequestMessage();
  PortalMessageService.getInstance().sendMessageToPortal(themeSwitchRequestMessage);
};

export const handleBreadcrumbs = (breadcrumbs: BreadcrumbInfo[]) => {
  const breadcrumbsMessage = new BreadcrumbsMessage(breadcrumbs);
  PortalMessageService.getInstance().sendMessageToPortal(breadcrumbsMessage);
  return breadcrumbs as React.ReactNode;
};

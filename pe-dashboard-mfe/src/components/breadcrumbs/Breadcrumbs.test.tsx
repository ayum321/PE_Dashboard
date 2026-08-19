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

import React from 'react';
import { render } from '@testing-library/react';
import { Breadcrumbs } from './Breadcrumbs';
import * as utils from '@jda/lui-portal-utilities';
import * as eventEmmiters from '../events-handler/EventEmmiters';

describe('Breadcrumbs', () => {
  let isIframeSpy: jest.SpyInstance<Boolean>;

  beforeEach(() => {
    jest.clearAllMocks();
    isIframeSpy = jest.spyOn(utils, 'isIFrame');
    jest.spyOn(eventEmmiters, 'handleBreadcrumbs').mockReturnValue([]);
  });

  it('should not render BreadcrumbsComponent', () => {
    isIframeSpy.mockReturnValue(false);
    const { queryByTestId } = render(<Breadcrumbs />);
    expect(queryByTestId('consumer-component')).toBeNull();
  });

  it('should render BreadcrumbsComponent', () => {
    isIframeSpy.mockReturnValue(true);
    const { container } = render(<Breadcrumbs />);
    const breadcrumbsContainer = container.querySelector('nav');
    expect(breadcrumbsContainer).toBeDefined();
  });
});

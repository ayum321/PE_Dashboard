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
import { Welcome } from './Welcome';

describe('Welcome', () => {
  it('should render', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByTestId } = render(<Welcome />);

    expect(getByTestId('lui-scaffolding-layouts-north-zone-child-container')).toBeDefined();
    expect(getByTestId('lui-scaffolding-layouts-north-zone-header-block')).toBeDefined();
    expect(getByTestId('lui-scaffolding-layouts-north-zone-header-block-title-container')).toBeDefined();
  });
});

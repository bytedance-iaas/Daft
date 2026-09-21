// Tests: the same handlers on MSW's Node interceptor.
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);

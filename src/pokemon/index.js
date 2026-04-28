import { registerPokemonCardRoutes } from './cards.js';
import { registerPokemonSetRoutes } from './sets.js';
import { registerPokemonImageRoutes } from './images.js';

export function registerPokemonRoutes(app) {
  registerPokemonCardRoutes(app);
  registerPokemonSetRoutes(app);
  registerPokemonImageRoutes(app);
}

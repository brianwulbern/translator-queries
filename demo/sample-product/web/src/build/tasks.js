const tasks = require('./runner');

tasks.register('clean', () => tasks.remove('dist'));

tasks.register('build', ['clean'], () => tasks.compile('src', 'dist'));

module.exports = {defaultTask: 'clean'};

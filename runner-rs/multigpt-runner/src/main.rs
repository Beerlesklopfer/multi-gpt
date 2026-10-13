//! Programm `multigpt-runner` (siehe [`multigpt_runner::cli`]).

use multigpt_runner::agent_handler::AgentHandler;
use multigpt_runner::cli;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let code = cli::main_with(args, AgentHandler::from_config);
    std::process::exit(code);
}
